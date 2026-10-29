# -*- coding: utf-8 -*-
"""采集引擎。独立线程里跑一个 asyncio 循环，所有浏览器操作都在这个循环里进行。"""
import asyncio
import io
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

from PIL import Image
from playwright.async_api import async_playwright

from . import store

# ---------------------------------------------------------------- page scripts
JS_MARK_SCROLLER = r"""
([cands, respCands]) => {
  document.querySelectorAll('[data-cc-scroll]').forEach(x => x.removeAttribute('data-cc-scroll'));
  const scrollable = e => /(auto|scroll|overlay)/.test(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight + 4;
  const sidebarLike = e => { const r = e.getBoundingClientRect();
    return !!e.closest('nav,aside,[class*="sidebar" i],[class*="sidenav" i],[class*="side-nav" i]') || (r.left < 40 && r.width < 420); };
  let el = null, how = null;
  for (const s of cands || []) {
    let e = null; try { e = document.querySelector(s); } catch (err) { continue; }
    if (e && e.scrollHeight > e.clientHeight + 4) { el = e; how = 'selector:' + s; break; }
  }
  // 从最新一条回答往上找能滚动的容器：长截图拍的一定是回答所在的区域，不会拍到侧边栏的历史对话
  if (!el) {
    let last = null;
    for (const s of respCands || []) { try { const all = document.querySelectorAll(s); if (all.length) { last = all[all.length - 1]; break; } } catch (err) {} }
    for (let e = last && last.parentElement; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
      if (scrollable(e)) { el = e; how = 'answer'; break; }
    }
  }
  if (!el) {
    const se = document.scrollingElement || document.documentElement;
    if (se.scrollHeight > innerHeight + 4) { el = se; how = 'page'; }
  }
  if (!el) {
    let best = null, bestScore = 0;
    for (const e of document.querySelectorAll('body *')) {
      if (!scrollable(e) || sidebarLike(e)) continue;
      const r = e.getBoundingClientRect();
      if (r.width < 280 || r.height < 200) continue;
      const score = r.width * r.height;
      if (score > bestScore) { best = e; bestScore = score; }
    }
    if (best) { el = best; how = 'auto'; }
  }
  if (!el) return null;
  el.setAttribute('data-cc-scroll', '1');
  return how;
}
"""

JS_SCROLL_INFO = r"""
() => {
  const e = document.querySelector('[data-cc-scroll]');
  if (!e) return null;
  const isPage = e === (document.scrollingElement || document.documentElement);
  const r = isPage ? {left: 0, top: 0, width: innerWidth} : e.getBoundingClientRect();
  const ch = isPage ? innerHeight : e.clientHeight;
  const keep = e.scrollTop;
  e.scrollTop = -1e9; const min = e.scrollTop;
  e.scrollTop = 1e9;  const max = e.scrollTop;
  e.scrollTop = keep;
  return {x: r.left, y: r.top, w: r.width, ch, min, max};
}
"""

JS_SCROLL_TO = r"""
(p) => { const e = document.querySelector('[data-cc-scroll]'); e.scrollTop = p; return e.scrollTop; }
"""

JS_HIDE_FLOATING = r"""
(hide) => {
  if (!hide) {
    document.querySelectorAll('[data-cc-hidden]').forEach(e => {
      e.style.visibility = e.getAttribute('data-cc-hidden'); e.removeAttribute('data-cc-hidden');
    });
    return 0;
  }
  const sc = document.querySelector('[data-cc-scroll]');
  let n = 0;
  for (const e of document.querySelectorAll('body *')) {
    const p = getComputedStyle(e).position;
    if (p !== 'fixed' && p !== 'sticky') continue;
    if (sc && e.contains(sc)) continue;
    e.setAttribute('data-cc-hidden', e.style.visibility || '');
    e.style.visibility = 'hidden'; n++;
  }
  return n;
}
"""

JS_FIRST = r"""
const firstOf = cands => { for (const s of cands || []) { try { const e = document.querySelector(s); if (e) return e; } catch (err) {} } return null; };
"""

JS_ROOT_LEN = r"""
(scrollCands) => {
  """ + JS_FIRST + r"""
  const root = firstOf(scrollCands) ||
               document.querySelector('[data-cc-scroll]') || document.querySelector('main') || document.body;
  const t = root.innerText || '';
  return {len: t.length, tail: t.slice(-400)};
}
"""

# 把回答的网页结构转成 Markdown：保留标题、列表、表格、代码块、加粗、链接和公式，
# 评审模型和导出的文本才能看出排版好坏。
JS_TO_MD = r"""
const toMd = (root) => {
  const SKIP = new Set(['BUTTON','SVG','STYLE','SCRIPT','NOSCRIPT','TEXTAREA','INPUT','SELECT','TEMPLATE','IFRAME','CANVAS','VIDEO','AUDIO']);
  const BLOCK = /^(block|flex|grid|list-item|table|flow-root)$/;
  const inl = s => s.replace(/\s+/g, ' ').trim();
  const wrap = (t, m) => { const x = t.match(/^(\s*)([\s\S]*?)(\s*)$/); return x[2] ? x[1] + m + x[2] + m + x[3] : t; };
  const tex = el => { const a = el.querySelector('annotation[encoding="application/x-tex"]'); return a ? a.textContent.trim() : null; };
  const kids = el => { let o = ''; for (const n of el.childNodes) o += walk(n); return o; };
  function walk(n) {
    if (n.nodeType === 3) return n.nodeValue.replace(/\s+/g, ' ');
    if (n.nodeType !== 1) return '';
    const el = n, tag = el.tagName.toUpperCase();
    if (SKIP.has(tag)) return '';
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return '';
    if (el.classList.contains('katex-display')) { const t = tex(el); if (t) return '\n\n$$' + t + '$$\n\n'; }
    if (el.classList.contains('katex')) { const t = tex(el); if (t) return '$' + t + '$'; }
    switch (tag) {
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6':
        return '\n\n' + '#'.repeat(+tag[1]) + ' ' + inl(kids(el)) + '\n\n';
      case 'P': return '\n\n' + kids(el) + '\n\n';
      case 'BR': return '\n';
      case 'HR': return '\n\n---\n\n';
      case 'STRONG': case 'B': return wrap(kids(el), '**');
      case 'EM': case 'I': return wrap(kids(el), '*');
      case 'DEL': case 'S': return wrap(kids(el), '~~');
      case 'CODE': return '`' + el.textContent + '`';
      case 'PRE': return codeBlock(el, '');
      case 'A': {
        const t = inl(kids(el));
        return (t && /^https?:/.test(el.href) && t !== el.href) ? '[' + t + '](' + el.href + ')' : t;
      }
      case 'IMG': {
        if ((el.naturalWidth || el.width) < 40 && (el.naturalHeight || el.height) < 40) return '';
        const src = el.currentSrc || el.src || '';
        return src && !src.startsWith('data:') ? '![' + (el.alt || '') + '](' + src + ')' : '[图片' + (el.alt ? '：' + el.alt : '') + ']';
      }
      case 'UL': case 'OL': {
        let i = parseInt(el.getAttribute('start') || '1', 10), o = '';
        for (const li of el.children) {
          if (li.tagName !== 'LI') { o += walk(li); continue; }
          const mark = tag === 'OL' ? (i++) + '. ' : '- ';
          const body = kids(li).replace(/\n\s*\n/g, '\n').trim();
          o += mark + body.replace(/\n/g, '\n' + ' '.repeat(mark.length)) + '\n';
        }
        return '\n\n' + o + '\n';
      }
      case 'TABLE': {
        const rows = [...el.querySelectorAll('tr')].filter(tr => tr.closest('table') === el);
        if (!rows.length) return kids(el);
        const cells = rows.map(tr => [...tr.children].filter(c => /^(TD|TH)$/.test(c.tagName))
          .map(c => inl(kids(c)).replace(/\|/g, '\\|')));
        const w = Math.max(...cells.map(r => r.length));
        const line = r => '| ' + Array.from({length: w}, (_, k) => r[k] || '').join(' | ') + ' |';
        return '\n\n' + [line(cells[0]), '|' + ' --- |'.repeat(w), ...cells.slice(1).map(line)].join('\n') + '\n\n';
      }
      case 'BLOCKQUOTE':
        return '\n\n' + kids(el).trim().split('\n').map(l => '> ' + l).join('\n') + '\n\n';
    }
    // 代码块外面常包一层带语言名和“复制”按钮的标题栏：只保留代码，语言名用作代码语言
    if (el !== root && el.querySelectorAll('pre').length === 1) {
      const ch = [...el.children], main = ch.find(c => c.tagName === 'PRE' || c.querySelector('pre'));
      const rest = ch.filter(c => c !== main), pre = el.querySelector('pre');
      const extra = (el.innerText || '').replace(pre.innerText || '', '').trim();
      if (main && rest.length && extra.length <= 30 && !rest.some(c => /^(P|H[1-6]|LI|UL|OL|TABLE|BLOCKQUOTE)$/.test(c.tagName))) {
        const m = extra.match(/^[A-Za-z][\w+#.-]{0,15}/);
        return codeBlock(pre, m ? m[0].toLowerCase() : '');
      }
    }
    // 表格外面带“表格”之类小标题栏的，只保留表格
    if (el !== root && el.querySelectorAll('table').length === 1 && !el.querySelector('pre')) {
      const tb = el.querySelector('table'), extra = (el.innerText || '').replace(tb.innerText || '', '').trim();
      if (extra.length <= 12 && ![...el.children].some(c => /^(P|H[1-6]|LI|UL|OL|BLOCKQUOTE)$/.test(c.tagName))) return walk(tb);
    }
    const t = kids(el);
    return BLOCK.test(cs.display) ? '\n' + t + '\n' : t;
  }
  function codeBlock(pre, label) {
    const code = pre.querySelector('code') || pre;
    const lang = ((code.className || '') + ' ' + (pre.className || '')).match(/language-([\w+#.-]+)/);
    return '\n\n```' + (lang ? lang[1] : label) + '\n' + code.textContent.replace(/\n+$/, '') + '\n```\n\n';
  }
  return walk(root).replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+(?=\n)/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
};
"""

# 按候选选择器找最新一条回答。排除思考过程、引用卡片等部分时，临时把它们设成不显示，读完马上恢复。
JS_EXTRACT_SEL = r"""
([cands, exclude, wantMd]) => {
  """ + JS_TO_MD + r"""
  const exSel = (exclude || []).filter(s => { try { document.querySelector(s); return true; } catch (e) { return false; } }).join(',');
  for (const s of cands || []) {
    let all; try { all = [...document.querySelectorAll(s)]; } catch (e) { continue; }
    all = all.filter(e => e.getClientRects().length && !(exSel && e.closest(exSel)));
    all = all.filter(e => !all.some(o => o !== e && o.contains(e)));
    if (!all.length) continue;
    const el = all[all.length - 1];
    const raw = (el.innerText || '').trim();
    const hidden = [];
    if (exSel) for (const x of el.querySelectorAll(exSel)) {
      hidden.push([x, x.style.getPropertyValue('display'), x.style.getPropertyPriority('display')]);
      x.style.setProperty('display', 'none', 'important');
    }
    let text = '', md = null;
    try {
      text = (el.innerText || '').trim();
      if (wantMd) md = toMd(el);
    } finally {
      for (const [x, v, p] of hidden) { if (v) x.style.setProperty('display', v, p); else x.style.removeProperty('display'); }
    }
    return {found: true, method: 'selector', sel: s, count: all.length, text, raw, md};
  }
  return {found: true, method: 'selector', sel: null, count: 0, text: '', raw: '', md: null};
}
"""

JS_EXTRACT = r"""
([scrollCands, queryHead, fromLen]) => {
  """ + JS_FIRST + r"""
  const root = firstOf(scrollCands) ||
               document.querySelector('[data-cc-scroll]') || document.querySelector('main') || document.body;
  const full = root.innerText || '';
  // 从发送前的文本末尾附近开始找问题，避免回答里引用了问题原文时截错位置
  let i = queryHead ? full.indexOf(queryHead, Math.max(0, (fromLen || 0) - 120)) : -1;
  if (i < 0 && queryHead) i = full.lastIndexOf(queryHead);
  if (i < 0) return {text: '', method: 'heuristic', found: false};
  return {text: full.slice(i + queryHead.length).trim(), method: 'heuristic', found: true};
}
"""

JS_SIGNATURE = r"""
() => { const t = document.body ? document.body.innerText || '' : ''; return t.length + ':' + t.slice(-300); }
"""

JS_PAGE_TEXT = r"""
() => (document.title || '') + '\n' + (document.body ? (document.body.innerText || '') : '')
"""

JS_CAPTCHA_FRAME = r"""
() => {
  for (const f of document.querySelectorAll('iframe')) {
    const s = (f.src || '') + ' ' + (f.title || '');
    if (/captcha|challenge|turnstile|geetest|recaptcha|hcaptcha|verify|punish|x5sec|nocaptcha|verifycenter/i.test(s)) {
      const r = f.getBoundingClientRect();
      if (r.width > 40 && r.height > 40) return s.slice(0, 80);
    }
  }
  // 不在 iframe 里的滑块/验证码（阿里 nc、字节 captcha_container 等）
  for (const e of document.querySelectorAll('#captcha_container,[id^="nc_"][id$="_wrapper"],.nc-container,.nc_wrapper,[class*="captcha-container" i],[class*="slider-verify" i],[id*="captcha" i]')) {
    const r = e.getBoundingClientRect();
    if (r.width > 40 && r.height > 30 && getComputedStyle(e).visibility !== 'hidden') return '验证码：' + (e.id || e.className || '').toString().slice(0, 40);
  }
  if (/\/sorry\/|\/punish/.test(location.pathname)) return '风控页面 ' + location.pathname.slice(0, 40);
  return null;
}
"""

JS_FIND_TOGGLE = r"""
([pattern, near]) => {
  let re; try { re = new RegExp(pattern, 'i'); } catch (e) { return {found: false}; }
  const alts = pattern.split('|').map(x => x.trim().toLowerCase());
  document.querySelectorAll('[data-cc-mode]').forEach(x => x.removeAttribute('data-cc-mode'));
  // near=true：只找输入框附近的按钮，避免点到侧边栏的“搜索对话”之类同名按钮
  let box = null;
  if (near) {
    const ins = [...document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]')]
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
    if (ins.length) box = ins[ins.length - 1].getBoundingClientRect();
  }
  const cands = [];
  const els = document.querySelectorAll('button,[role="button"],[role="switch"],[role="checkbox"],[role="menuitem"],a,label,div,span');
  for (const e of els) {
    const t = ((e.innerText || '').trim() || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim();
    if (!t || t.length > 16 || !re.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (box && (r.top - box.bottom > 220 || box.top - r.bottom > 220)) continue;
    const exact = alts.includes(t.toLowerCase());
    const clickable = e.matches('button,[role="button"],[role="switch"],[role="checkbox"],[role="menuitem"],a,label') ||
                      !!e.closest('button,[role="button"],[role="switch"],[role="checkbox"],[role="menuitem"],a,label');
    cands.push({e, t, score: (exact ? 0 : 100) + (clickable ? 0 : 50) + t.length});
  }
  // 外层容器和里面的按钮文字相同时，只保留最里面的那个
  const inner = cands.filter(c => !cands.some(o => o !== c && c.e.contains(o.e)));
  if (!inner.length) return {found: false};
  inner.sort((a, b) => a.score - b.score);
  const {e, t} = inner[0];
  const c = e.closest('button,[role="button"],[role="switch"],[role="checkbox"],[role="menuitem"],a,label') || e;
  c.setAttribute('data-cc-mode', '1');
  const vals = ['aria-pressed','aria-checked','aria-selected','data-state','data-checked','data-active','data-selected']
    .map(a => c.getAttribute(a)).filter(v => v != null).map(String);
  let state = null;
  if (vals.some(v => /^(true|on|checked|active|selected|1)$/i.test(v))) state = true;
  else if (vals.some(v => /^(false|off|unchecked|inactive|0)$/i.test(v))) state = false;
  else {
    const cls = typeof c.className === 'string' ? c.className : (c.className && c.className.baseVal) || '';
    if (/(^|[\s_-])(active|selected|checked|is-on|enabled|open)([\s_-]|$)/i.test(cls)) state = true;
  }
  return {found: true, state, text: t};
}
"""

# 侧边栏里是账号的历史对话，属于隐私，采集时收起；收不起来就模糊处理，保证截图里看不到
JS_SIDEBAR_PANELS = r"""
const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.right > 4 && r.left < innerWidth - 4 && getComputedStyle(e).visibility !== 'hidden'; };
const panels = (sels) => {
  let list = [];
  for (const s of sels || []) { try { list.push(...document.querySelectorAll(s)); } catch (e) {} }
  // 内置选择器没匹配到（比如登录前后页面结构不同）时，按位置找：靠左、窄而高的面板（左边可能还有一列图标栏）
  if (!list.length) list = [...document.querySelectorAll('nav,aside,[class*="sidebar" i],[class*="sidenav" i],[class*="side-nav" i],[class*="sider" i],bard-sidenav')]
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 150 && r.width < 460 && r.height > 300 && r.left < 120; });
  // 收起后剩下的窄图标栏不算；收起后元素可能还在，只是被移出屏幕或被裁掉，所以用点击测试确认真的看得见
  const shown = e => { const r = e.getBoundingClientRect(), x0 = Math.max(r.left, 0), x1 = Math.min(r.right, innerWidth);
    if (x1 - x0 < 150) return false;
    const h = document.elementFromPoint((x0 + x1) / 2, Math.min(r.top + r.height / 2, innerHeight - 2));
    return !!h && (e.contains(h) || h.contains(e)) && +getComputedStyle(e).opacity > 0.1; };
  list = list.filter(e => vis(e) && shown(e));
  return list.filter(e => !list.some(o => o !== e && o.contains(e)));
};
"""

JS_SIDEBAR_CLOSE = r"""
([closeSels, panelSels]) => {
  """ + JS_SIDEBAR_PANELS + r"""
  if (!panels(panelSels).length) return 'closed';
  const RE = /^(关闭边栏|收起边栏|关闭侧边栏|收起侧边栏|关闭侧栏|收起侧栏|隐藏侧边栏|折叠侧边栏|显示\/隐藏侧边栏|切换侧边栏|收起导航|关闭导航|收起菜单|Close sidebar|Collapse sidebar|Hide sidebar|Toggle sidebar|Close menu)$/i;
  let btn = null;
  for (const s of closeSels || []) { try { btn = [...document.querySelectorAll(s)].find(vis); } catch (e) {} if (btn) break; }
  if (!btn) btn = [...document.querySelectorAll('button,[role="button"],[aria-label]')]
    .find(e => vis(e) && RE.test((e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || '').trim()));
  if (!btn) return 'no_button';
  btn.click();
  return 'clicked';
}
"""

JS_SIDEBAR_BLUR = r"""
(panelSels) => {
  """ + JS_SIDEBAR_PANELS + r"""
  const ps = panels(panelSels);
  for (const e of ps) { e.style.setProperty('filter', 'blur(9px)', 'important'); e.style.setProperty('pointer-events', 'none', 'important'); }
  return ps.length;
}
"""

# 模型/模式选择：按选择器（可加文字过滤）找元素，标记出来交给 Playwright 点击；同时读出开关状态
JS_PICK = r"""
([sels, pattern, onClass, onAttr]) => {
  document.querySelectorAll('[data-cc-pick]').forEach(x => x.removeAttribute('data-cc-pick'));
  let re = null; if (pattern) { try { re = new RegExp(pattern, 'i'); } catch (e) {} }
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const label = e => ((e.innerText || '').trim() || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim();
  for (const s of sels || []) {
    let all; try { all = [...document.querySelectorAll(s)]; } catch (e) { continue; }
    all = all.filter(vis).filter(e => !re || re.test(label(e)) || re.test(e.getAttribute('aria-label') || ''));
    if (!all.length) continue;
    const c = all[0];
    c.setAttribute('data-cc-pick', '1');
    let state = null;
    const vals = ['aria-pressed','aria-checked','aria-selected','data-state','data-checked','data-active','data-selected']
      .map(a => c.getAttribute(a)).filter(v => v != null).map(String);
    const cls = typeof c.className === 'string' ? c.className : '';
    if (onClass) state = cls.includes(onClass);
    else if (onAttr) state = c.getAttribute(onAttr[0]) === onAttr[1];
    else if (vals.some(v => /^(true|on|checked|active|selected|1)$/i.test(v))) state = true;
    else if (vals.some(v => /^(false|off|unchecked|inactive|0)$/i.test(v))) state = false;
    else if (/(^|[\s_-])(active|selected|checked|is-on)([\s_-]|$)/i.test(cls)) state = true;
    return {found: true, state, text: label(c).slice(0, 60), expanded: c.getAttribute('aria-expanded')};
  }
  return {found: false};
}
"""

# 在菜单项里找和选项名最匹配的一个：完全相同 > 某个词相同 > 开头相同 > 包含；同分取文字最短（最里层）的
JS_PICK_ITEM = r"""
([sels, choice]) => {
  document.querySelectorAll('[data-cc-pick]').forEach(x => x.removeAttribute('data-cc-pick'));
  const want = String(choice).trim().toLowerCase();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  for (const s of sels || []) {
    let all; try { all = [...document.querySelectorAll(s)]; } catch (e) { continue; }
    let best = null, bs = 1e9;
    for (const e of all.filter(vis)) {
      const t = (e.innerText || e.getAttribute('aria-label') || '').trim().toLowerCase();
      if (!t || t.length > 80) continue;
      const first = t.split('\n')[0].trim();
      let sc = t === want || first === want ? 0 : t.split(/\s+/).includes(want) ? 1 : first.startsWith(want) ? 2 : t.includes(want) ? 3 : null;
      if (sc === null) continue;
      sc = sc * 1000 + t.length;
      if (sc < bs) { bs = sc; best = e; }
    }
    if (best) {
      best.setAttribute('data-cc-pick', '1');
      const checked = ['aria-checked', 'aria-selected', 'aria-pressed'].some(a => best.getAttribute(a) === 'true') || /(^|[\s_-])(checked|selected|active)([\s_-]|$)/i.test(typeof best.className === 'string' ? best.className : '');
      return {found: true, text: (best.innerText || '').trim().slice(0, 60), checked};
    }
  }
  return {found: false};
}
"""

# 滑杆当前这一格的名字：ChatGPT 放在 role=status 里，形如“High，第 3 项，共 5 项。”
JS_SLIDER_LABEL = r"""
(sl, pos) => {
  // 页面上可能同时有好几个滑杆的状态文字（不同菜单视图各一个），只读离这个滑杆最近的那个
  let p = sl;
  for (let i = 0; i < 10 && p; i++) {
    p = p.parentElement;
    const s = p && p.querySelector('[role=status]');
    if (s) { const t = (s.textContent || '').trim(); return t.includes('第 ' + pos + ' 项') ? t.split(/[，,]/)[0].trim() : ''; }
  }
  return '';
}
"""

# 读当前菜单里的选项名：取第一个能匹配到可见元素的选择器，每项取第一行文字，同名只留最里层
JS_READ_ITEMS = r"""
(sels) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  for (const s of sels || []) {
    let all; try { all = [...document.querySelectorAll(s)].filter(vis); } catch (e) { continue; }
    const rows = all.map(e => ({e, t: (e.innerText || e.getAttribute('aria-label') || '').trim().split('\n')[0].trim()}))
      .filter(x => x.t && x.t.length <= 30);
    const keep = rows.filter(x => !rows.some(o => o !== x && x.e.contains(o.e) && o.t === x.t));
    const names = [...new Set(keep.map(x => x.t))];
    if (names.length) return names.slice(0, 20);
  }
  return [];
}
"""

# 平铺的标签/单选（如“聊天/工作”）：找到和已知选项同名的元素，读它同一组里的兄弟元素，顺便读出当前选中的
JS_SCAN_RADIO = r"""
([sels, known]) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const txt = e => (e.innerText || '').trim().split('\n')[0].trim();
  const on = e => ['aria-pressed', 'aria-checked', 'aria-selected'].some(a => e.getAttribute(a) === 'true')
    || /(^|[\s_-])(active|selected|checked)([\s_-]|$)/i.test(typeof e.className === 'string' ? e.className : '');
  for (const s of sels || []) {
    let all; try { all = [...document.querySelectorAll(s)].filter(vis); } catch (e) { continue; }
    const hit = all.find(e => known.includes(txt(e)));
    if (!hit) continue;
    const group = [...hit.parentElement.children].filter(e => vis(e) && txt(e) && txt(e).length <= 12);
    return {choices: [...new Set(group.map(txt))], current: (group.find(on) && txt(group.find(on))) || null};
  }
  return {choices: [], current: null};
}
"""

# 录制：记下用户在页面上的每次点击（点在输入框里的不算），存在 sessionStorage 里，页面内跳转也不丢
JS_RECORDER = r"""
(() => {
  if (window.__ccRecOn) return; window.__ccRecOn = true;
  const keep = ['data-testid','data-test-id','aria-label','role','aria-haspopup','dt-button-id','data-model-name','data-mode-id','data-chat-mode','data-moon-key','data-composer-navigation-target','value'];
  document.addEventListener('click', ev => {
    const raw = ev.target;
    if (!raw || !raw.closest || raw.closest('textarea,input,[contenteditable="true"]')) return;
    const e = raw.closest('button,a,li,label,[role],gem-menu-item,[tabindex],[data-testid]') || raw;
    const text = (e.innerText || '').trim().split('\n')[0].trim().slice(0, 40);
    const attrs = {};
    for (const k of keep) { const v = e.getAttribute && e.getAttribute(k); if (v && v.length < 80 && !/^radix-|^_r_/.test(v)) attrs[k] = v; }
    if (!text && !attrs['aria-label'] && !attrs['data-testid']) return;
    const list = JSON.parse(sessionStorage.getItem('__ccrec') || '[]');
    const r = e.getBoundingClientRect();
    list.push({text, tag: e.tagName.toLowerCase(), attrs, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), at: Date.now()});
    sessionStorage.setItem('__ccrec', JSON.stringify(list));
  }, true);
})();
"""

# 回放录制的一步：先按稳定属性 + 文字找，再只按文字找可点的元素
JS_FIND_STEP = r"""
(st) => {
  document.querySelectorAll('[data-cc-pick]').forEach(x => x.removeAttribute('data-cc-pick'));
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const txt = e => (e.innerText || '').trim().split('\n')[0].trim();
  const a = st.attrs || {};
  const strong = ['data-testid','data-test-id','dt-button-id','data-model-name','data-mode-id','data-chat-mode','data-moon-key','data-composer-navigation-target','value','aria-label']
    .filter(k => a[k]).map(k => `[${k}="${CSS.escape(a[k])}"]`);
  const pools = [];
  if (strong.length) pools.push(strong.join(''), ...strong);
  if (a.role) pools.push(`[role="${a.role}"]`);
  pools.push(st.tag || '*', 'button,[role],a,li,label,gem-menu-item,[tabindex]');
  for (const sel of pools) {
    let all; try { all = [...document.querySelectorAll(sel)].filter(vis); } catch (e) { continue; }
    let c = st.text ? all.filter(e => txt(e) === st.text) : all;
    if (!c.length && st.text && sel === pools[0] && strong.length) c = all;   // 文字变了但属性能对上
    if (c.length) { const e = c[c.length - 1]; e.setAttribute('data-cc-pick', '1'); return true; }
  }
  // 下拉按钮上显示的是“当前选中的名字”，回放时可能已经变了：找录制位置附近同类的下拉按钮
  if (a['aria-haspopup'] && st.x != null) {
    let best = null, bd = 160;
    for (const e of document.querySelectorAll(`${st.tag || 'button'}[aria-haspopup]`)) {
      if (!vis(e)) continue;
      const r = e.getBoundingClientRect(), d = Math.hypot(r.left + r.width / 2 - st.x, r.top + r.height / 2 - st.y);
      if (d < bd) { bd = d; best = e; }
    }
    if (best) { best.setAttribute('data-cc-pick', '1'); return true; }
  }
  return false;
}
"""

# 菜单项不在预设的位置时，在所有打开着的菜单/弹层里按名字找
POPUP_ITEMS = ['[role="menu"] *', '[role="listbox"] *', '[role="dialog"] *', "[data-radix-popper-content-wrapper] *",
               ".cdk-overlay-pane *", '[class*="popover" i] *', '[class*="dropdown" i] *']

# 关掉页面加载后弹出的引导/公告：按钮文字完全一致才点
JS_DISMISS = r"""
(pattern) => {
  const alts = pattern.split('|').map(x => x.trim()).filter(Boolean);
  let n = 0;
  for (const e of document.querySelectorAll('button,[role="button"],a,span,div')) {
    const t = (e.innerText || '').trim();
    if (!alts.includes(t) || !e.getClientRects().length) continue;
    if ([...e.children].some(c => (c.innerText || '').trim() === t)) continue;   // 只点最里层
    (e.closest('button,[role="button"],a') || e).click(); n++;
  }
  return n;
}
"""

GENERIC_INPUTS = ['textarea', 'div[contenteditable="true"]', '[role="textbox"]']
GENERIC_SEND = ('button[aria-label*="Send" i], button[aria-label*="发送"], '
                'button[data-testid*="send" i], button[type="submit"]')
GENERIC_STOP = ('button[aria-label*="Stop" i], button[aria-label*="停止"], '
                '[data-testid*="stop" i], button[aria-label*="中止"]')

VERIFY_PATTERNS = ["Just a moment", "Verify you are human", "verify you are human", "确认您是真人", "请完成安全验证",
                   "拖动滑块", "向右滑动", "人机验证", "请完成验证", "Checking your browser", "安全验证",
                   "滑块验证", "拖动下方滑块", "按住滑块", "请完成下列验证", "点击按钮进行验证", "访问异常", "检测到异常",
                   "异常流量", "unusual traffic", "Unusual traffic", "security verification", "Security verification"]
LIMIT_PATTERNS = ["使用上限", "次数已用完", "已达上限", "已达到上限", "额度已用完", "用量已达", "请求过于频繁",
                  "操作太频繁", "操作过于频繁", "访问过于频繁", "Too many requests", "too many requests",
                  "rate limit", "usage limit", "You've reached", "You’ve reached", "reached the limit",
                  "hit the Free plan limit", "hit your limit", "out of free messages"]
BUSY_PATTERNS = ["服务器繁忙", "服务繁忙", "系统繁忙", "网络错误", "Something went wrong", "出错了，请重试",
                 "Network error", "生成失败"]
LOGIN_HINTS = ["登录", "Log in", "Sign in", "注册"]
# 发送后弹出的登录框：通用识别会把这些文字误当成回答
LOGIN_WALL_PATTERNS = ["扫码登录", "手机号码登录", "手机号登录", "验证码登录", "请先登录", "登录后即可", "登录以继续",
                       "Log in or sign up", "Sign in to continue", "Log in to continue"]
UI_TAIL_WORDS = {"发送", "复制", "重新生成", "分享", "编辑", "停止", "点赞", "点踩", "朗读", "Copy", "Retry",
                 "Share", "Edit", "Regenerate", "Send", "Stop"}

MOBILE_DEVICE = "iPhone 13"
# 多个浏览器窗口互相遮挡或锁屏时，Chrome 默认会降低后台窗口的渲染和计时器频率，
# 导致回答“看起来停了”被误判为结束。这些参数关闭这种节流。
CHROME_ARGS = ["--disable-blink-features=AutomationControlled", "--disable-backgrounding-occluded-windows",
               "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
RECYCLE_EVERY = 40          # 每问这么多条换一个新标签页，避免长时间运行内存越积越多
MAX_FAILS_IN_ROW = 5        # 连续失败这么多条就暂停该产品，等人确认
AUTO_RESUME_KINDS = {"verify", "login", "blocked"}   # 这几类人工处理完、页面恢复正常后自动继续
GENERATING_HINTS = ["思考中", "正在思考", "深度思考中", "正在搜索", "搜索中", "联网搜索中", "正在生成", "生成中",
                    "正在阅读", "正在查找", "正在分析", "Thinking", "Searching", "Reasoning"]
PAGE_LIMIT_PATTERNS = ["次数已用完", "额度已用完", "已达到使用上限", "使用次数已达上限", "hit the Free plan limit",
                       "You've reached", "You’ve reached", "out of free messages", "hit your limit"]


class Attention(Exception):
    """需要人工介入：登录、验证码、额度用完。"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Busy(Exception):
    """站点暂时繁忙，等一会儿重试。"""


class Halted(Exception):
    """用户点了停止或跳过。"""


def effective_wait(cfg, s, key=None):
    """深度思考/联网搜索/切换了模型时回答中途常有长时间停顿，自动放宽结束判定。"""
    w = dict(s["wait"])
    chosen = {k: v for k, v in ((cfg.get("site_opts") or {}).get(key) or {}).items() if v not in (None, "", False)}
    if (cfg.get("mode") and cfg["mode"] != "standard") or chosen:
        w["stable"] = max(float(w["stable"]), 8)
        w["max"] = max(float(w["max"]), 420)
    return w


def query_timeout(cfg, s, q, key=None):
    w = effective_wait(cfg, s, key)
    return (float(w["max"]) + 60) * len(q.get("turns") or [1]) + 180


def clean_text(t, footer=""):
    tail = set(UI_TAIL_WORDS)
    for x in (footer or "").splitlines():
        x = x.strip()
        if x:
            tail.add(x)
            tail.update(w for w in x.split() if len(w) <= 12)
    lines = (t or "").rstrip().splitlines()
    while lines and (not lines[-1].strip() or lines[-1].strip() in tail):
        lines.pop()
    return "\n".join(lines).strip()


def find_pattern(text, pats):
    for p in pats:
        if p in text:
            return p
    return None


def notify(title, msg):
    if not store.get_settings().get("notify", True):
        return
    if sys.platform == "darwin":
        t = title.replace('"', "'")
        m = msg.replace('"', "'")
        try:
            subprocess.Popen(["osascript", "-e", f'display notification "{m}" with title "{t}" sound name "Glass"'])
        except Exception:
            pass


class JobCtrl:
    def __init__(self):
        self.run_ev = asyncio.Event()
        self.run_ev.set()
        self.stop_ev = asyncio.Event()
        self.cont = {}
        self.skip = {}      # site -> Event，跳过该产品剩余的问题

    def skip_ev(self, key):
        if key not in self.skip:
            self.skip[key] = asyncio.Event()
        return self.skip[key]

    def halted(self, key):
        return self.stop_ev.is_set() or self.skip_ev(key).is_set()

    @property
    def stopped(self):
        return self.stop_ev.is_set()


async def wait_any(*events, timeout=None):
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


class Engine:
    def __init__(self):
        self.loop = None
        self.queue = None
        self.pw = None
        self.ctrl = {}
        self.active = None
        self.queued = []
        self.login_ctx = {}
        self.busy_sites = set()
        self.exclusive = set()      # 正在检查或试跑的产品，期间任务里的这个产品先等着，避免两个浏览器抢同一份登录数据
        self.probing = set()
        self.lane_pages = {}        # (任务, 产品) -> 当前页面，用于“切到浏览器窗口”
        self.recording = {}         # 产品 -> 正在录制点选的浏览器
        self.ready = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True, name="engine").start()
        self.ready.wait(60)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.pw = await async_playwright().start()
        for j in store.list_jobs():
            if j["status"] in ("running", "queued", "needs_attention", "stopping", "paused"):
                def upd(job):
                    job["status"] = "paused"
                    for lane in job["lanes"].values():
                        if lane["state"] not in ("done",):
                            lane["state"] = "paused"
                            lane["attention"] = None
                            lane["message"] = "平台重启后已暂停，点“继续”接着跑"
                store.update_job(j["id"], upd)
            if (j.get("analysis") or {}).get("state") in ("running", "writing"):
                store.update_job(j["id"], lambda job: job["analysis"].update(
                    state="error", message="平台重启，分析中断了。点“开始 AI 分析”会接着分析没评完的部分"))
        self.ready.set()
        while True:
            jid = await self.queue.get()
            if jid in self.queued:
                self.queued.remove(jid)
            job = store.get_job(jid)
            if not job or job["status"] == "stopped":
                continue
            self.active = jid
            try:
                await self.run_job(jid)
            except Exception as e:
                traceback.print_exc()
                store.update_job(jid, lambda j: j.update(status="failed", error=str(e)))
            finally:
                self.active = None
                self.ctrl.pop(jid, None)

    def call(self, coro, timeout=120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _soon(self, fn):
        self.loop.call_soon_threadsafe(fn)

    # ------------------------------------------------------------ public controls (thread-safe)
    def submit(self, jid):
        store.update_job(jid, lambda j: j.update(status="queued"))
        self.queued.append(jid)
        self._soon(lambda: self.queue.put_nowait(jid))

    def pause(self, jid):
        c = self.ctrl.get(jid)
        if c:
            self._soon(c.run_ev.clear)
            store.update_job(jid, lambda j: j.update(status="paused"))

    def resume(self, jid):
        c = self.ctrl.get(jid)
        if c:
            self._soon(c.run_ev.set)
            store.update_job(jid, lambda j: j.update(status="running"))
        elif jid not in self.queued:
            self.submit(jid)

    def stop(self, jid):
        c = self.ctrl.get(jid)
        if c:
            self._soon(c.stop_ev.set)
            store.update_job(jid, lambda j: j.update(status="stopping"))
        else:
            if jid in self.queued:
                self.queued.remove(jid)

            def upd(j):
                j["status"] = "stopped"
                for lane in j["lanes"].values():
                    if lane["state"] != "done":
                        lane["state"] = "stopped"
                        lane["attention"] = None
            store.update_job(jid, upd)

    def continue_lane(self, jid, site):
        c = self.ctrl.get(jid)
        if c and site in c.cont:
            self._soon(c.cont[site].set)

    def skip_lane(self, jid, site):
        c = self.ctrl.get(jid)
        if not c:
            return False

        def go():
            c.skip_ev(site).set()
            if site in c.cont:
                c.cont[site].set()
        self._soon(go)
        return True

    def status(self):
        return {"active": self.active, "queued": list(self.queued), "login_open": list(self.login_ctx),
                "busy_sites": list(self.busy_sites), "probing": list(self.probing), "recording": list(self.recording)}

    # ------------------------------------------------------------ browser
    async def launch(self, site, device="desktop", headless=None, guest=False):
        """guest=True：不带任何登录状态和缓存的全新浏览器（相当于浏览器无痕窗口）。"""
        s = store.get_settings()
        profile = store.PROFILES / site["key"]
        if not guest:
            profile.mkdir(parents=True, exist_ok=True)
        kw = dict(user_data_dir=str(profile),
                  headless=s["browser"]["headless"] if headless is None else headless,
                  locale="zh-CN", args=CHROME_ARGS)
        if device == "mobile":
            d = dict(self.pw.devices[MOBILE_DEVICE])
            d.pop("default_browser_type", None)
            kw.update(d)
            kw["device_scale_factor"] = min(float(s["shot"]["dpr"]) + 0.5, 3)
        else:
            kw.update(viewport={"width": 1280, "height": 900}, device_scale_factor=float(s["shot"]["dpr"]))
        if s["browser"].get("channel"):
            kw["channel"] = s["browser"]["channel"]
        if guest:
            bkw = {k: kw.pop(k) for k in ("headless", "args", "channel") if k in kw}
            kw.pop("user_data_dir")
            try:
                browser = await self.pw.chromium.launch(**bkw)
            except Exception:
                bkw.pop("channel", None)
                browser = await self.pw.chromium.launch(**bkw)
            ctx = await browser.new_context(**kw)
            ctx.on("close", lambda *_: asyncio.ensure_future(browser.close()))
            return ctx
        try:
            return await self.pw.chromium.launch_persistent_context(**kw)
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in ("ProcessSingleton", "already in use", "SingletonLock", "profile appears to be in use")):
                # 上次浏览器异常退出留下的锁文件，清掉后重试一次
                for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                    try:
                        (profile / name).unlink()
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass
                store.log(None, site["key"], "清理了上次遗留的浏览器锁文件，重新启动")
                return await self.pw.chromium.launch_persistent_context(**kw)
            if kw.get("channel"):
                store.log(None, site["key"], f"无法用本机 {kw['channel']} 启动（{str(e).splitlines()[0][:120]}），改用内置浏览器")
                kw.pop("channel")
                return await self.pw.chromium.launch_persistent_context(**kw)
            raise

    async def open_login(self, key):
        if key in self.busy_sites:
            raise RuntimeError("这个产品正在采集中，等任务结束或暂停后再登录")
        if key in self.login_ctx:
            return
        site = next(s for s in store.get_sites() if s["key"] == key)
        ctx = await self.launch(site, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(site["url"], wait_until="domcontentloaded")
        self.login_ctx[key] = ctx
        ctx.on("close", lambda *_: self.login_ctx.pop(key, None))

    async def close_login(self, key):
        ctx = self.login_ctx.pop(key, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    async def check_site(self, key):
        if key in self.busy_sites or key in self.exclusive:
            return store.site_status().get(key, {})
        await self.close_login(key)
        site = next(s for s in store.get_sites() if s["key"] == key)
        store.set_site_status(key, "checking", "检查中…")
        ctx = None
        self.exclusive.add(key)
        try:
            ctx = await self.launch(site)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(4)
            text = await page.evaluate(JS_PAGE_TEXT)
            v = find_pattern(text, VERIFY_PATTERNS) or await page.evaluate(JS_CAPTCHA_FRAME)
            if v:
                state, msg = "blocked", f"页面出现验证（{v}），点“登录”手动处理一次"
            else:
                box, _ = await first_visible(page, sel_list(site, "input") + GENERIC_INPUTS)
                if not box:
                    state, msg = "need_login", "没找到输入框，多半是未登录"
                elif find_pattern(text[:4000], LOGIN_HINTS):
                    state, msg = "guest", "能找到输入框，但页面上有登录入口，可能是未登录状态"
                else:
                    state, msg = "ready", "可以采集"
        except Exception as e:
            state, msg = "error", "打不开页面：" + str(e).splitlines()[0][:150]
        finally:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            self.exclusive.discard(key)
        store.set_site_status(key, state, msg)
        return store.site_status().get(key)


    # ------------------------------------------------------------ probe
    async def probe(self, key, question, device="desktop", opts=None):
        """试跑一条：用这个产品的登录状态问一个问题，记录每一步用了哪个选择器、取到了什么，用来检查适配是否还有效。
        结果写到 data/probe/<产品>/result.json，截图在同一目录。"""
        if key in self.busy_sites:
            raise RuntimeError("这个产品正在采集中，等任务结束或暂停后再试跑")
        if key in self.exclusive:
            raise RuntimeError("这个产品正在检查或试跑")
        site = next(s for s in store.get_sites() if s["key"] == key)
        out = store.probe_dir(key)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        s = store.get_settings()
        cfg = {"mode": "standard", "device": device, "incognito": bool(s.get("incognito_default", True)),
               "profile": "account", "shot": "both", "site_opts": {key: opts} if opts else {}}
        q = {"id": "probe", "query": question, "turns": [question], "category": "试跑"}
        result = {"state": "running", "site": key, "question": question, "device": device,
                  "started_at": store.now_iso(), "selectors": site["sel"], "preset_info": site.get("preset_info")}
        store.write_json(out / "result.json", result)
        self.exclusive.add(key)
        self.probing.add(key)
        await self.close_login(key)
        ctx = page = None
        diag = {}
        try:
            ctx = await self.launch(site, device)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            rec = await asyncio.wait_for(self.do_query(None, page, site, q, cfg, {"busy": False}, base=out, diag=diag),
                                         query_timeout(cfg, s, q, key))
            h = await extract(page, site, question, 0)
            diag["heuristic_text"] = clean_text(h.get("text") or "", "")[:6000]
            result.update(state="done", rec=rec)
        except Attention as a:
            result.update(state="error", message=a.message, kind=a.kind)
        except Exception as e:
            result.update(state="error", message=(str(e).splitlines()[0][:300] if str(e) else e.__class__.__name__))
        finally:
            if result["state"] == "error" and page is not None:
                try:
                    await page.screenshot(path=str(out / "error.jpg"), type="jpeg", quality=75)
                    result["error_shot"] = "error.jpg"
                except Exception:
                    pass
            if ctx:
                try:
                    await asyncio.wait_for(ctx.close(), 20)
                except Exception:
                    pass
            self.exclusive.discard(key)
            self.probing.discard(key)
            result.update(diag=diag, finished_at=store.now_iso())
            store.write_json(out / "result.json", result)
        return result

    # ------------------------------------------------------------ job
    async def run_job(self, jid):
        job = store.get_job(jid)
        cfg = job["config"]
        ctrl = JobCtrl()
        self.ctrl[jid] = ctrl
        sites = {s["key"]: s for s in store.get_sites()}
        lane_sites = [sites[k] for k in cfg["sites"] if k in sites]

        def start(j):
            j["status"] = "running"
            j.setdefault("started_at", store.now_iso())
            for k, lane in j["lanes"].items():
                if lane["state"] != "done" or lane["done"] < lane["total"]:
                    lane.update(state="starting", message="", attention=None)
        store.update_job(jid, start)
        store.log(jid, "任务", f"开始：{job['name']}")
        for s in lane_sites:
            await self.close_login(s["key"])
        await asyncio.gather(*(self.lane(jid, s, cfg, ctrl) for s in lane_sites))
        final = "stopped" if ctrl.stopped else "done"
        job = store.update_job(jid, lambda j: j.update(status=final, finished_at=store.now_iso()))
        p = job["progress"]
        store.log(jid, "任务", f"结束：成功 {p['ok']}，失败 {p['failed']}，共 {p['total']}")
        try:
            from .compose import compose_job
            await asyncio.to_thread(compose_job, jid)
        except Exception as e:
            store.log(jid, "任务", f"生成对比图失败：{e}")
        notify("采集完成" if final == "done" else "采集已停止", f"{job['name']}：成功 {p['ok']} / {p['total']}")
        if final == "done" and cfg.get("auto_analyze"):
            from .analyzer import start_analysis
            start_analysis(jid)

    def lane_set(self, jid, key, **kw):
        def upd(j):
            j["lanes"][key].update(kw)
            states = [l["state"] for l in j["lanes"].values()]
            if j["status"] in ("running", "needs_attention"):
                j["status"] = "needs_attention" if "needs_human" in states else "running"
        store.update_job(jid, upd)

    async def lane(self, jid, site, cfg, ctrl):
        key = site["key"]
        s = store.get_settings()
        pacing = cfg["pacing"]
        headless = s["browser"].get("headless")
        halt = [ctrl.stop_ev, ctrl.skip_ev(key)]
        results = store.get_results(jid)
        done_ok = {r["query_id"] for r in results["results"] if r["site"] == key and r["status"] == "ok"}
        todo = [q for q in cfg["queries"] if q["id"] not in done_ok]
        if not todo:
            self.lane_set(jid, key, state="done", current=None, message="")
            return
        if key in self.exclusive:
            self.lane_set(jid, key, state="starting", message="这个产品正在检查或试跑，结束后自动开始")
            while key in self.exclusive and not ctrl.halted(key):
                await asyncio.sleep(2)
        self.busy_sites.add(key)
        st = {"ctx": None, "page": None, "used": 0}
        live = {"stop": False, "busy": False, "page": None}
        live_task = None

        async def ensure_page():
            """确认浏览器和页面还活着；被关掉或崩溃了就重新打开。定期换新标签页释放内存。"""
            page = st["page"]
            alive = page is not None and not page.is_closed()
            if alive:
                try:
                    await asyncio.wait_for(page.evaluate("1"), 8)
                except Exception:
                    alive = False
            if alive and st["used"] >= RECYCLE_EVERY:
                try:
                    newp = await st["ctx"].new_page()
                    await page.close()
                    st["page"] = live["page"] = self.lane_pages[(jid, key)] = newp
                    st["used"] = 0
                    return
                except Exception:
                    alive = False
            if alive:
                return
            if st["ctx"] is not None:
                store.log(jid, key, "浏览器窗口被关闭或无响应，重新打开")
                try:
                    await asyncio.wait_for(st["ctx"].close(), 15)
                except Exception:
                    pass
            self.lane_set(jid, key, state="starting", message="正在打开浏览器")
            st["ctx"] = await self.launch(site, cfg.get("device", "desktop"), guest=cfg.get("profile") == "guest")
            st["page"] = st["ctx"].pages[0] if st["ctx"].pages else await st["ctx"].new_page()
            live["page"] = st["page"]
            self.lane_pages[(jid, key)] = st["page"]
            st["used"] = 0

        async def wait_human(kind, msg):
            """暂停这个产品，请人到它的浏览器窗口里接管处理，处理完点“继续”或“跳过”。
            验证码/登录/弹窗这几类会自动检测：页面恢复正常（验证消失、能找到输入框）就自动继续。
            返回 False 表示应结束该产品。"""
            if headless:
                return False
            ev = asyncio.Event()
            ctrl.cont[key] = ev
            auto = kind in AUTO_RESUME_KINDS
            self.lane_set(jid, key, state="needs_human", message=msg,
                          attention={"kind": kind, "message": msg, "auto": auto, "since": store.now_iso()})
            notify("需要你接管一下", f"{site['name']}：{msg}")
            await self.focus_page(st["page"])
            ok_streak = 0
            while not (ev.is_set() or ctrl.halted(key)):
                await wait_any(ev, *halt, timeout=3)
                if ev.is_set() or ctrl.halted(key) or not auto:
                    continue
                ok_streak = ok_streak + 1 if await self.looks_resolved(st["page"], site) else 0
                if ok_streak >= 2:
                    store.log(jid, key, "检测到页面已恢复正常，自动继续")
                    break
            ctrl.cont.pop(key, None)
            if ctrl.halted(key):
                return False
            self.lane_set(jid, key, state="running", attention=None, message="继续")
            return True

        try:
            try:
                await ensure_page()
            except Exception as e:
                self.lane_set(jid, key, state="error", message="浏览器启动失败：" + str(e).splitlines()[0][:200])
                store.log(jid, key, f"浏览器启动失败：{e}")
                return
            live_task = asyncio.create_task(self.live_loop(jid, key, live))
            since_rest = fails_in_row = 0
            for n, q in enumerate(todo):
                if ctrl.halted(key):
                    break
                if not ctrl.run_ev.is_set():
                    self.lane_set(jid, key, state="paused", message="已暂停")
                    await wait_any(ctrl.run_ev, *halt)
                    if ctrl.halted(key):
                        break
                label = q["id"] + (f"（第 {q['run']} 次）" if q.get("run", 1) > 1 else "")
                self.lane_set(jid, key, state="running", current=q["id"], current_text=q["query"][:80],
                              message=f"正在问 {label}", attention=None)
                rec = None
                attempts = 0
                while not ctrl.halted(key):
                    try:
                        await ensure_page()
                        st["used"] += 1
                        rec = await self.guarded(self.do_query(jid, st["page"], site, q, cfg, live), halt,
                                                 query_timeout(cfg, s, q, key))
                        break
                    except Halted:
                        break
                    except Attention as a:
                        store.log(jid, key, f"{q['id']} 需要人工处理：{a.message}")
                        if headless:
                            rec = self.err_rec(site, q, cfg, f"需要人工处理但处于无界面模式：{a.message}")
                            break
                        if not await wait_human(a.kind, a.message):
                            break
                        if a.kind == "risk":
                            q = {**q, "risk_ack": True}     # 人确认过了，重问后还是这句就照常记录，不再暂停
                    except Busy as b:
                        attempts += 1
                        store.log(jid, key, f"{q['id']} 站点繁忙（{b}），{60 * attempts} 秒后重试")
                        if attempts > pacing.get("retries", 1) + 1:
                            rec = self.err_rec(site, q, cfg, f"站点持续繁忙：{b}")
                            break
                        self.lane_set(jid, key, message=f"站点繁忙，{60 * attempts} 秒后重试")
                        await wait_any(*halt, timeout=60 * attempts)
                    except Exception as e:
                        attempts += 1
                        err = (str(e).splitlines()[0][:300] if str(e) else e.__class__.__name__)
                        store.log(jid, key, f"{q['id']} 第 {attempts} 次失败：{err}")
                        if attempts > pacing.get("retries", 1):
                            rec = self.err_rec(site, q, cfg, err)
                            await self.error_shot(jid, st["page"], key, q, rec)
                            break
                        await asyncio.sleep(3)
                if rec is None:
                    break
                store.put_result(jid, rec)
                secs = (rec.get("total_ms") or 0) / 1000
                store.log(jid, key, f"{q['id']} {rec['status']}  {secs:.1f}s  {len(rec.get('response_text') or '')} 字"
                          + (f"  ⚠ {rec['warning']}" if rec.get("warning") else ""))
                fails_in_row = fails_in_row + 1 if rec["status"] == "error" else 0
                if fails_in_row >= MAX_FAILS_IN_ROW:
                    msg = (f"已经连续 {fails_in_row} 条失败（最近一次：{(rec.get('error') or '')[:80]}）。"
                           "可能是页面改版、登录失效或网络问题。看一下浏览器窗口，处理后点继续，或跳过这个产品")
                    fails_in_row = 0
                    if not await wait_human("fails", msg):
                        break
                if rec.get("warning"):
                    if not await wait_human("limit", f"{rec['warning']}。后面的回答可能被降级或失败，确认后点继续"):
                        break
                since_rest += 1
                if n < len(todo) - 1 and not ctrl.halted(key):
                    bs = int(pacing.get("batch_size") or 0)
                    if bs and since_rest >= bs:
                        mins = float(pacing.get("batch_rest_min") or 0)
                        until = time.time() + mins * 60
                        self.lane_set(jid, key, state="resting",
                                      message=f"已连续问 {since_rest} 条，休息到 {time.strftime('%H:%M', time.localtime(until))}")
                        await wait_any(*halt, timeout=mins * 60)
                        since_rest = 0
                    else:
                        lo, hi = float(pacing.get("delay_min", 3)), float(pacing.get("delay_max", 8))
                        self.lane_set(jid, key, state="waiting", message="间隔等待中")
                        await wait_any(*halt, timeout=random.uniform(lo, max(lo, hi)))
            j = store.get_job(jid)
            lane = j["lanes"][key]
            if ctrl.skip_ev(key).is_set():
                final = "skipped"
            elif ctrl.stopped and lane["done"] < lane["total"]:
                final = "stopped"
            else:
                final = "done"
            self.lane_set(jid, key, state=final, current=None, current_text="",
                          message="已跳过剩余问题" if final == "skipped" else "", attention=None)
        except Exception as e:
            traceback.print_exc()
            self.lane_set(jid, key, state="error", message=str(e)[:200], attention=None)
        finally:
            live["stop"] = True
            if live_task:
                live_task.cancel()
            self.busy_sites.discard(key)
            self.lane_pages.pop((jid, key), None)
            if st["ctx"]:
                try:
                    await asyncio.wait_for(st["ctx"].close(), 20)
                except Exception:
                    pass

    async def guarded(self, coro, halt, timeout):
        """执行单条采集：设了总超时（防止页面卡死导致整条线停住），点停止/跳过时立即中断。"""
        task = asyncio.ensure_future(coro)
        waiters = [asyncio.ensure_future(e.wait()) for e in halt]
        try:
            done, _ = await asyncio.wait([task, *waiters], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        if task in done:
            return task.result()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        if any(e.is_set() for e in halt):
            raise Halted()
        raise TimeoutError(f"单条超过 {int(timeout)} 秒仍未完成，已放弃（页面可能卡住了）")

    async def live_loop(self, jid, key, live):
        path = store.job_dir(jid) / "live" / f"{key}.jpg"
        path.parent.mkdir(exist_ok=True)
        while not live["stop"]:
            if not live["busy"]:
                try:
                    await live["page"].screenshot(path=str(path), type="jpeg", quality=45, scale="css", timeout=4000)
                except Exception:
                    pass
            await asyncio.sleep(2.5)

    def base_rec(self, site, q, cfg):
        return {"site": site["key"], "site_name": site["name"], "query_id": q["id"], "base_id": q.get("base_id"),
                "run": q.get("run", 1), "query": q["query"], "category": q["category"], "turns": [],
                "status": "error", "response_text": "", "response_md": None, "ttft_ms": None, "answer_start_ms": None,
                "total_ms": None, "text_method": None, "screenshot": None,
                "window_screenshot": None, "url": None, "error": None, "warning": None,
                "mode": cfg.get("mode"), "mode_state": {}, "device": cfg.get("device"), "incognito": None, "sidebar": None,
                "captured_at": store.now_iso()}

    def err_rec(self, site, q, cfg, err):
        rec = self.base_rec(site, q, cfg)
        rec["error"] = err
        return rec

    async def error_shot(self, jid, page, key, q, rec):
        try:
            p = store.job_dir(jid) / "shots" / key / f"{safe(q['id'])}_error.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(p), type="jpeg", quality=70)
            rec["window_screenshot"] = rec["screenshot"] = p.relative_to(store.job_dir(jid)).as_posix()
        except Exception:
            pass

    # ------------------------------------------------------------ one query
    async def do_query(self, jid, page, site, q, cfg, live, base=None, diag=None):
        """问一条（可能多轮）。base：截图保存的根目录，默认是任务目录；diag：试跑时收集每一步的识别情况。"""
        s = store.get_settings()
        w = effective_wait(cfg, s, site["key"])
        base = base or store.job_dir(jid)
        url = (site.get("mobile_url") or site["url"]) if cfg.get("device") == "mobile" else site["url"]
        rec = self.base_rec(site, q, cfg)
        inc = cfg.get("incognito") and cfg.get("profile") != "guest"
        if inc and site.get("incognito_url"):
            url = site["incognito_url"]
        # 问题直接放进网址（如 Google AI Mode）；要先选模型时改成打开首页、选好再输入
        direct = bool(site.get("query_url")) and cfg.get("device") != "mobile" and not (cfg.get("site_opts") or {}).get(site["key"])
        if not direct:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2.5)
            await self.check_verify(page)
        await self.prepare_page(jid, page, site, cfg, rec, diag, skip=direct)
        use_sel = bool(sel_list(site, "response"))
        total_ms = 0
        methods = []
        for ti, text in enumerate(q["turns"]):
            if direct and ti == 0:
                sel_base, pre, base_len = {"count": 0, "raw": "", "text": "", "sel": None}, {"len": 0, "tail": ""}, 0
                t_sent, how = time.monotonic(), "网址直接提问"
                await page.goto(site["query_url"].replace("{q}", urllib.parse.quote(text)), wait_until="domcontentloaded", timeout=60000)
                await self.check_verify(page)
                if diag is not None:
                    diag["send"], diag["input"] = how, "（网址提问，不需要输入框）"
            else:
                box, in_sel = await first_visible(page, sel_list(site, "input") + GENERIC_INPUTS)
                if diag is not None and ti == 0:
                    diag["input"] = in_sel
                if not box:
                    await self.check_verify(page)
                    raise Attention("login", "没找到输入框：可能未登录或弹出了提示。请在浏览器窗口里处理好，再点继续")
                pre = await page.evaluate(JS_ROOT_LEN, sel_list(site, "scroll"))
                base_len = pre["len"]
                sel_base = await extract_sel(page, site) if use_sel else None
                t_sent, how = await send_text(page, site, box, text)
            if diag is not None and ti == 0:
                diag["send"] = how
                diag["response_before"] = sel_base and {"count": sel_base["count"], "sel": sel_base["sel"]}
            if not use_sel:
                sel_base = None
            wr = await wait_answer(page, site, text, t_sent, w, self, base_len, pre["tail"], sel_base, diag)
            if direct and ti == 0 and s.get("hide_sidebar", True):
                rec["sidebar"] = await self.hide_sidebar(page, site)
            md = None
            got = await extract_sel(page, site, md=True) if wr["by_selector"] else None
            if got and (got.get("text") or "").strip():
                method = "selector"
                md = got.get("md")
            else:
                got = await extract(page, site, text, base_len)
                method = "heuristic_fallback" if use_sel else "heuristic"
            if method == "heuristic_fallback":
                store.log(jid, site["key"], f"{q['id']} 回答选择器没取到这条回答，改用通用识别。"
                                            "可能是页面改版了，到“产品 → 试跑”检查一下")
            if diag is not None and ti == 0:
                diag["response_after"] = {"count": (got or {}).get("count"), "sel": (got or {}).get("sel")}
                diag["method"] = method
            resp = clean_text(got.get("text", "") or "", wr["footer"])
            risk = site.get("risk_answers") or []
            if risk and not q.get("risk_ack") and len(resp) < 60 and find_pattern(resp, risk):
                raise Attention("risk", f"{site['name']} 回复了“{resp[:30]}”，多半是触发了风控（也可能是正常拒答）。"
                                        "可以在浏览器窗口里换个节奏手动问一次确认；点继续会重问这一条，若仍是这句就照常记录")
            if method != "selector" and is_part_of(resp, text):
                raise RuntimeError("没取到回答：取到的只是问题本身，可能没发出去或页面没有出现回答")
            if method != "selector":
                wall = find_pattern(resp, LOGIN_WALL_PATTERNS)
                if wall:
                    raise Attention("login", f"发送后页面弹出了登录框（“{wall}”）。请在浏览器窗口里登录，再点继续")
            if len(resp) < 200:
                b = find_pattern(resp, BUSY_PATTERNS)
                if b:
                    raise Busy(b)
                lim = find_pattern(resp, LIMIT_PATTERNS)
                if lim:
                    raise Attention("limit", f"站点提示额度或频率受限（{lim}）。可以换账号、稍后再试，或处理后点继续")
            if wr["status"] == "no_answer":
                raise RuntimeError("发送后没有出现回答")
            rec["turns"].append({"query": text, "response": resp, "response_md": md, "ttft_ms": ms(wr["ttft"]),
                                 "answer_start_ms": ms(wr["answer_at"]), "total_ms": ms(wr["total"]),
                                 "status": wr["status"], "text_method": method})
            total_ms += ms(wr["total"]) or 0
            methods.append(method)
            if ti == 0:
                rec["ttft_ms"] = ms(wr["ttft"])
                rec["answer_start_ms"] = ms(wr["answer_at"])
        rec["text_method"] = methods[0] if len(set(methods)) == 1 else "mixed"
        rec["total_ms"] = total_ms
        rec["status"] = "timeout" if any(t["status"] == "timeout" for t in rec["turns"]) else "ok"
        if rec["status"] == "timeout":
            rec["error"] = f"超过 {int(w['max'])} 秒仍未结束，已按当时的状态截图"
        turns = rec["turns"]
        if len(turns) == 1:
            rec["response_text"] = turns[0]["response"]
            rec["response_md"] = turns[0]["response_md"]
        else:
            rec["response_text"] = "\n\n".join(f"【第 {i + 1} 轮】问：{t['query']}\n答：{t['response']}"
                                               for i, t in enumerate(turns))
            if any(t["response_md"] for t in turns):
                rec["response_md"] = "\n\n".join(f"**【第 {i + 1} 轮】问：{t['query']}**\n\n{t['response_md'] or t['response']}"
                                                 for i, t in enumerate(turns))
        rec["url"] = page.url
        page_text = await page.evaluate(JS_PAGE_TEXT)
        for t in rec["turns"]:
            for part in (t["response"], t["query"]):
                if part:
                    page_text = page_text.replace(part, "")
        lim = find_pattern(page_text, PAGE_LIMIT_PATTERNS)
        if lim:
            rec["warning"] = f"页面出现额度/限流提示“{lim}”"
        await self.take_shots(jid, page, site, q, cfg, rec, live, base, diag)
        return rec

    async def hide_sidebar(self, page, site):
        """收起侧边栏（历史对话）。返回 closed（本来就没显示）/ collapsed（点按钮收起了）/ blurred（收不起来，已模糊）/ failed。"""
        close, panel = sel_list(site, "sidebar_close"), sel_list(site, "sidebar")
        try:
            st = await page.evaluate(JS_SIDEBAR_CLOSE, [close, panel])
            if st == "closed":
                return "closed"
            if st == "clicked":
                for _ in range(5):       # 等收起动画
                    await asyncio.sleep(0.5)
                    if await page.evaluate(JS_SIDEBAR_CLOSE, [[], panel]) == "closed":
                        return "collapsed"
            return "blurred" if await page.evaluate(JS_SIDEBAR_BLUR, panel) else "closed"
        except Exception:
            return "failed"

    async def focus_page(self, page):
        """把需要人工处理的浏览器窗口切到前台。"""
        try:
            if page is not None and not page.is_closed():
                await page.bring_to_front()
        except Exception:
            pass

    def focus_lane(self, jid, key):
        page = self.lane_pages.get((jid, key))
        if page is None:
            return False
        self._soon(lambda: asyncio.ensure_future(self.focus_page(page)))
        return True

    async def looks_resolved(self, page, site):
        """人工处理后页面是否恢复正常：没有验证码/风控提示，没有登录框，能找到输入框。"""
        try:
            if page is None or page.is_closed():
                return False
            text = await page.evaluate(JS_PAGE_TEXT)
            if find_pattern(text[:6000], VERIFY_PATTERNS) or await page.evaluate(JS_CAPTCHA_FRAME):
                return False
            if find_pattern(text, LOGIN_WALL_PATTERNS):
                return False
            box, _ = await first_visible(page, sel_list(site, "input") + GENERIC_INPUTS)
            return box is not None
        except Exception:
            return False

    async def check_verify(self, page):
        text = await page.evaluate(JS_PAGE_TEXT)
        v = find_pattern(text[:6000], VERIFY_PATTERNS) or await page.evaluate(JS_CAPTCHA_FRAME)
        if v:
            raise Attention("verify", f"页面出现验证（{v}）。请切到弹出的浏览器窗口完成验证，再点继续")

    async def toggle(self, page, pattern, on, near=True):
        """按按钮文字找到开关并切到目标状态。返回 on / off（带 ? 表示状态无法确认）/ not_found 等。"""
        if not pattern:
            return "not_configured"
        info = await page.evaluate(JS_FIND_TOGGLE, [pattern, near])
        if not info.get("found"):
            return "not_found" if on else "off"
        state = info.get("state")
        if (on and state is not True) or (not on and state is True):
            try:
                await page.locator("[data-cc-mode]").first.click(timeout=3000)
                await asyncio.sleep(0.6)
            except Exception:
                return "click_failed"
        return ("on" if on else "off") + ("" if state is not None else "?")

    async def prepare_page(self, jid, page, site, cfg, rec, diag, skip=False):
        """问之前的准备：关弹窗、收侧边栏、进无痕对话、选模型/模式。网址直接提问的产品跳过。"""
        if skip:
            return
        s = store.get_settings()
        inc = cfg.get("incognito") and cfg.get("profile") != "guest"
        if site.get("dismiss"):
            n = await page.evaluate(JS_DISMISS, site["dismiss"])
            if n:
                await asyncio.sleep(0.8)
                if diag is not None:
                    diag["dismissed"] = n
        if s.get("hide_sidebar", True) and cfg.get("device") != "mobile":
            rec["sidebar"] = await self.hide_sidebar(page, site)
            if diag is not None:
                diag["sidebar"] = rec["sidebar"]
        if cfg.get("profile") == "guest":
            rec["incognito"] = "guest"
        elif inc:
            if site.get("incognito_url"):
                rec["incognito"] = "url"
            else:
                st = await self.toggle(page, site.get("incognito_button") or "", True, near=False)
                rec["incognito"] = st
                if st in ("not_found", "not_configured", "click_failed"):
                    store.log(jid, site["key"], "没找到无痕/临时对话入口，按普通对话采集（结果里已标注）")
                else:
                    await asyncio.sleep(1.2)
        chosen = (cfg.get("site_opts") or {}).get(site["key"]) or {}
        if chosen:
            rec["options"] = await self.apply_options(page, site, chosen)
            bad = {o["label"]: o["state"] for o in rec["options"].values() if not o["state"].startswith(("on", "off", "ok"))}
            if bad:
                store.log(jid, site["key"], f"有的模型/模式没选上：{bad}，按页面当前状态继续（结果里已标注）")
            if diag is not None:
                diag["options"] = rec["options"]
        elif cfg.get("mode") and cfg["mode"] != "standard":
            rec["mode_state"] = await self.apply_mode(page, site, cfg["mode"])
            if any(v == "not_found" for v in rec["mode_state"].values()):
                store.log(jid, site["key"], f"没找到模式开关：{rec['mode_state']}，按页面默认模式继续")

    async def apply_options(self, page, site, chosen):
        """按任务里给这个产品选的模型/模式逐项设置。返回 {选项id: {label, want, state}}。
        state：on/off（开关，带 ? 表示状态无法确认）、ok/ok?（菜单已选，? 表示选后无法确认）、
        not_found_trigger / not_found_submenu / not_found_item / not_found / click_failed / error。"""
        out = {}
        for opt in site.get("options") or []:
            want = chosen.get(opt["id"])
            if want in (None, "", "default"):
                continue
            await close_menus(page)
            try:
                if opt["kind"] == "steps":
                    st = await self.replay_steps(page, opt["steps"]) if want else "off"
                elif opt["kind"] == "toggle":
                    st = await self.opt_toggle(page, site, opt, bool(want))
                else:
                    st = await self.opt_menu(page, opt, str(want))
            except Exception as e:
                st = "error"
                store.log(None, site["key"], f"选择 {opt['label']} 出错：{str(e).splitlines()[0][:120]}")
            await close_menus(page)
            out[opt["id"]] = {"label": opt["label"], "want": want, "state": st}
            await asyncio.sleep(1.5 if opt["kind"] == "radio" else 0.5)   # 切模式（如“聊天/工作”）后页面会重新渲染
        return out

    async def opt_toggle(self, page, site, opt, on):
        if opt.get("text_field"):
            return await self.toggle(page, site.get(opt["text_field"]) or "", on)
        args = [opt.get("selector") or [], opt.get("text"), opt.get("on_class"), opt.get("on_attr")]
        info = await page.evaluate(JS_PICK, args)
        if not info.get("found"):
            return "not_found" if on else "off?"
        if info.get("state") is not on:
            try:
                await page.locator("[data-cc-pick]").first.click(timeout=4000)
            except Exception:
                return "click_failed"
            await asyncio.sleep(0.7)
            info = await page.evaluate(JS_PICK, args)
        state = info.get("state")
        if state is None:
            return ("on" if on else "off") + "?"
        return ("on" if state else "off") + ("" if state is on else "（没切换成功）")

    async def opt_menu(self, page, opt, want):
        trig = opt.get("trigger") or []

        async def wait_pick(js, args, secs):
            """页面刚切换模式、菜单刚打开时元素可能还没渲染出来，等一会儿再判定找不到。"""
            end = time.monotonic() + secs
            while True:
                r = await page.evaluate(js, args)
                if r.get("found") or time.monotonic() > end:
                    return r
                await asyncio.sleep(0.5)
        if trig:
            t = await wait_pick(JS_PICK, [trig, opt.get("trigger_text"), None, None], 6)
            if not t.get("found"):
                return "not_found_trigger"
            await page.locator("[data-cc-pick]").first.click(timeout=4000)
            await asyncio.sleep(0.8)
            if opt.get("submenu"):
                sm = await wait_pick(JS_PICK, [opt["submenu"], opt.get("submenu_text"), None, None], 0 if opt.get("submenu_optional") else 3)
                if sm.get("found"):
                    await page.locator("[data-cc-pick]").first.click(timeout=4000)
                    await asyncio.sleep(0.8)
                elif not opt.get("submenu_optional"):
                    await page.keyboard.press("Escape")
                    return "not_found_submenu"
        if opt.get("slider"):
            r = await self.opt_slider(page, opt, want)
            if r != "no_slider":
                await page.keyboard.press("Escape")      # ChatGPT 关掉菜单时才保存选择
                await asyncio.sleep(0.6)
                return r
        it = await wait_pick(JS_PICK_ITEM, [opt.get("items") or [], want], 3)
        if not it.get("found"):
            it = await page.evaluate(JS_PICK_ITEM, [POPUP_ITEMS, want])     # 不在预设位置，就在所有弹层里按名字找
        if not it.get("found"):
            if trig:
                await page.keyboard.press("Escape")
            return "not_found_item"
        try:
            await page.locator("[data-cc-pick]").first.click(timeout=4000)
        except Exception:
            if trig:
                await page.keyboard.press("Escape")
            return "click_failed"
        await asyncio.sleep(0.7)
        if not trig:      # 平铺的选项（标签/单选）：点完再看一眼是否变成选中
            await asyncio.sleep(0.6)
            again = await page.evaluate(JS_PICK_ITEM, [opt.get("items") or [], want])
            return "ok" if again.get("checked") else "ok?"
        if trig:
            t = await page.evaluate(JS_PICK, [trig, opt.get("trigger_text"), None, None])
            if t.get("expanded") == "true":
                await page.keyboard.press("Escape")      # 有的产品（如 ChatGPT）关掉菜单时才保存选择
                await asyncio.sleep(0.5)
                t = await page.evaluate(JS_PICK, [trig, opt.get("trigger_text"), None, None])
            if want.lower() in (t.get("text") or "").lower():
                return "ok"
        return "ok?"

    async def opt_slider(self, page, opt, want):
        """菜单里是滑杆（如 ChatGPT 思考强度）：一格一格移动，读每一格的名字，停在想要的那一格。"""
        sl = page.locator(", ".join(opt["slider"])).first
        try:
            if not await sl.is_visible():
                return "no_slider"
        except Exception:
            return "no_slider"
        lo = int(await sl.get_attribute("aria-valuemin") or 0)
        hi = int(await sl.get_attribute("aria-valuemax") or 0)
        await sl.focus()

        async def label(n):
            for _ in range(10):      # 等名字刷新到这一格（状态文字更新有延迟）
                await asyncio.sleep(0.3)
                if await sl.get_attribute("aria-valuenow") != str(n):
                    continue
                t = (await sl.get_attribute("aria-valuetext")) or await sl.evaluate(JS_SLIDER_LABEL, n + 1 - lo)
                if t:
                    return t
            return ""
        for _ in range(hi - lo + 1):
            await sl.press("Home" if _ == 0 else "ArrowLeft")
            await asyncio.sleep(0.3)
            if await sl.get_attribute("aria-valuenow") == str(lo):
                break
        seen = []
        w = want.strip().lower()
        for n in range(lo, hi + 1):
            if n > lo:
                await sl.press("ArrowRight")
            t = await label(n)
            seen.append(t)
            if t and (t.strip().lower() == w or t.strip().lower().startswith(w)):
                return "ok"
        return "not_found_item"

    async def replay_steps(self, page, steps):
        """回放录制的点选步骤。每步最多等 5 秒元素出现。"""
        for i, stp in enumerate(steps):
            end = time.monotonic() + 5
            while not await page.evaluate(JS_FIND_STEP, stp):
                if time.monotonic() > end:
                    return f"not_found_step:{i + 1}（{stp.get('text') or stp.get('attrs', {}).get('aria-label') or '?'}）"
                await asyncio.sleep(0.5)
            try:
                await page.locator("[data-cc-pick]").first.click(timeout=4000)
            except Exception:
                return f"click_failed:{i + 1}"
            await asyncio.sleep(0.9)
        await close_menus(page)
        return "ok"

    # ------------------------------------------------------------ 从页面读取最新的模型/模式选项
    async def read_slider(self, page, opt):
        sl = page.locator(", ".join(opt["slider"])).first
        if not await sl.is_visible():
            return []
        lo = int(await sl.get_attribute("aria-valuemin") or 0)
        hi = int(await sl.get_attribute("aria-valuemax") or 0)
        orig = int(await sl.get_attribute("aria-valuenow") or lo)
        await sl.focus()
        for _ in range(hi - lo + 1):
            await sl.press("ArrowLeft"); await asyncio.sleep(0.3)
        names = []
        for n in range(lo, hi + 1):
            if n > lo:
                await sl.press("ArrowRight")
            for _ in range(10):
                await asyncio.sleep(0.3)
                if await sl.get_attribute("aria-valuenow") == str(n):
                    t = (await sl.get_attribute("aria-valuetext")) or await sl.evaluate(JS_SLIDER_LABEL, n + 1 - lo)
                    if t:
                        names.append(t); break
        for _ in range(hi - orig):            # 回到原来的那一格
            await sl.press("ArrowLeft"); await asyncio.sleep(0.4)
        if len(names) != hi - lo + 1 or len(set(names)) != len(names):
            return []                         # 读到的名字缺了或重复（页面状态文字没跟上），宁可不更新
        return names

    async def read_menu(self, page, opt):
        """打开一个下拉（含子菜单），读出里面的选项名，然后关掉，不改任何选择。"""
        trig = opt.get("trigger") or []
        if trig:
            t = None
            for _ in range(12):
                t = await page.evaluate(JS_PICK, [trig, opt.get("trigger_text"), None, None])
                if t.get("found"):
                    break
                await asyncio.sleep(0.5)
            if not t.get("found"):
                return None
            await page.locator("[data-cc-pick]").first.click(timeout=4000)
            await asyncio.sleep(0.9)
            if opt.get("slider"):
                names = await self.read_slider(page, opt)
                await page.keyboard.press("Escape"); await asyncio.sleep(0.5)
                await close_menus(page)
                return names
            if opt.get("submenu"):
                sm = await page.evaluate(JS_PICK, [opt["submenu"], opt.get("submenu_text"), None, None])
                if sm.get("found"):
                    await page.locator("[data-cc-pick]").first.click(timeout=4000)
                    await asyncio.sleep(0.9)
                elif not opt.get("submenu_optional"):
                    await close_menus(page)
                    return None
        names = await page.evaluate(JS_READ_ITEMS, opt.get("items") or [])
        await close_menus(page)
        return names

    async def scan_options(self, key):
        """用登录状态打开产品页面，按内置适配逐项读出当前的模型/模式名称；有“模式决定可选模型”的，逐个模式切过去读，最后切回原来的模式。
        结果写到 data/probe/<产品>/options_scan.json，由用户确认后保存。"""
        if key in self.busy_sites or key in self.exclusive:
            raise RuntimeError("这个产品正在采集、检查或试跑，等结束后再更新选项")
        site = next(s for s in store.get_sites() if s["key"] == key)
        path = store.probe_dir(key) / "options_scan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        res = {"state": "running", "started_at": store.now_iso(), "options": {}}
        store.write_json(path, res)
        self.exclusive.add(key)
        self.probing.add(key)
        await self.close_login(key)
        ctx = None
        try:
            ctx = await self.launch(site, "desktop", headless=False)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            await self.check_verify(page)
            if site.get("dismiss"):
                await page.evaluate(JS_DISMISS, site["dismiss"])
            await self.hide_sidebar(page, site)
            opts = [o for o in site["options"] if o["kind"] != "steps"]
            out = {}
            radios = {}
            for o in opts:
                if o["kind"] == "radio":
                    r = await page.evaluate(JS_SCAN_RADIO, [o.get("items") or [], o.get("choices") or []])
                    radios[o["id"]] = (o, r)
                    out[o["id"]] = {"label": o["label"], "kind": "radio", "choices": r["choices"], "current": r["current"],
                                    "found": bool(r["choices"])}
                elif o["kind"] == "toggle":
                    if o.get("text_field"):
                        info = await page.evaluate(JS_FIND_TOGGLE, [site.get(o["text_field"]) or "", True])
                    else:
                        info = await page.evaluate(JS_PICK, [o.get("selector") or [], o.get("text"), o.get("on_class"), o.get("on_attr")])
                    out[o["id"]] = {"label": o["label"], "kind": "toggle", "found": bool(info.get("found")), "state": info.get("state")}
            for o in opts:
                if o["kind"] != "menu":
                    continue
                dep = (o.get("choices_by") or {}).get("on")
                if dep in radios and radios[dep][1]["choices"]:
                    ro, rr = radios[dep]
                    m = {}
                    for val in rr["choices"]:
                        await self.opt_menu(page, ro, val)
                        await asyncio.sleep(1.5)
                        names = await self.read_menu(page, o)
                        if names:
                            m[val] = names
                    if rr["current"]:
                        await self.opt_menu(page, ro, rr["current"])
                        await asyncio.sleep(1.2)
                    out[o["id"]] = {"label": o["label"], "kind": "menu", "choices_by": m, "found": bool(m)}
                else:
                    names = await self.read_menu(page, o)
                    out[o["id"]] = {"label": o["label"], "kind": "menu", "choices": names or [], "found": bool(names)}
            res.update(state="done", options=out)
        except Attention as a:
            res.update(state="error", message=a.message)
        except Exception as e:
            res.update(state="error", message=(str(e).splitlines()[0][:300] if str(e) else e.__class__.__name__))
        finally:
            if ctx:
                try:
                    await asyncio.wait_for(ctx.close(), 20)
                except Exception:
                    pass
            self.exclusive.discard(key)
            self.probing.discard(key)
            res["finished_at"] = store.now_iso()
            store.write_json(path, res)
        return res

    # ------------------------------------------------------------ 录制一组点选
    async def start_record(self, key):
        if key in self.busy_sites or key in self.exclusive:
            raise RuntimeError("这个产品正在采集、检查或试跑，等结束后再录制")
        site = next(s for s in store.get_sites() if s["key"] == key)
        await self.close_login(key)
        self.exclusive.add(key)
        try:
            ctx = await self.launch(site, "desktop", headless=False)
            await ctx.add_init_script(JS_RECORDER)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(site["url"], wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            if site.get("dismiss"):
                await page.evaluate(JS_DISMISS, site["dismiss"])
            await page.evaluate("() => sessionStorage.removeItem('__ccrec')")
            await page.evaluate(JS_RECORDER)
            self.recording[key] = {"ctx": ctx, "page": page}
        except Exception:
            self.exclusive.discard(key)
            raise

    async def stop_record(self, key, save=True):
        r = self.recording.pop(key, None)
        if not r:
            raise RuntimeError("没有在录制")
        steps = []
        try:
            steps = await r["page"].evaluate("() => JSON.parse(sessionStorage.getItem('__ccrec') || '[]')")
        except Exception:
            pass
        try:
            await asyncio.wait_for(r["ctx"].close(), 15)
        except Exception:
            pass
        self.exclusive.discard(key)
        clean = []
        for st in steps:                       # 连着点同一个东西只留一次
            if clean and clean[-1]["text"] == st.get("text") and clean[-1]["attrs"] == st.get("attrs"):
                continue
            clean.append({k: st.get(k) for k in ("text", "tag", "attrs", "x", "y")})
        return clean[:20]

    async def apply_mode(self, page, site, mode):
        want = {"reasoning": mode in ("reasoning", "reasoning_search"), "search": mode in ("search", "reasoning_search")}
        return {name: await self.toggle(page, site.get(f"mode_{name}") or "", on) for name, on in want.items()}

    async def take_shots(self, jid, page, site, q, cfg, rec, live, base=None, diag=None):
        mode = cfg.get("shot", "both")
        if mode == "off":
            return
        s = store.get_settings()
        base = base or store.job_dir(jid)
        d = base / "shots" / site["key"]
        d.mkdir(parents=True, exist_ok=True)
        name = safe(q["id"])
        if mode in ("both", "window"):
            p = d / f"{name}_window.jpg"
            await page.screenshot(path=str(p), type="jpeg", quality=80)
            rec["window_screenshot"] = p.relative_to(base).as_posix()
        if mode in ("both", "long"):
            live["busy"] = True
            try:
                p = await long_screenshot(page, site, d / name, s["shot"], diag)
                rec["screenshot"] = p.relative_to(base).as_posix()
            except Exception as e:
                store.log(jid, site["key"], f"长截图失败（{e}），改用窗口截图")
                if diag is not None:
                    diag["long_shot_error"] = str(e).splitlines()[0][:200]
                p = d / f"{name}_window.jpg"
                if not p.exists():
                    await page.screenshot(path=str(p), type="jpeg", quality=80)
                rec["screenshot"] = p.relative_to(base).as_posix()
            finally:
                live["busy"] = False
        if not rec["screenshot"]:
            rec["screenshot"] = rec["window_screenshot"]


# ---------------------------------------------------------------- helpers
def safe(qid):
    return re.sub(r"[^0-9A-Za-z_-]", "_", qid)


def ms(sec):
    return round(sec * 1000) if sec is not None else None


async def close_menus(page):
    """把还开着的下拉菜单关掉，免得挡住下一步要点的按钮。"""
    for _ in range(3):
        try:
            open_ = await page.evaluate("""() => [...document.querySelectorAll('[role="menu"],[role="listbox"],[data-radix-popper-content-wrapper]')]
              .some(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })""")
        except Exception:
            return
        if not open_:
            return
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)


def sel_list(site, kind):
    """引擎实际使用的选择器候选：用户填的优先，没填用内置适配（见 store.resolve_site）。"""
    return list((site.get("sel") or {}).get(kind) or [])


async def first_visible(page, selectors):
    for sel in selectors:
        if not sel:
            continue
        loc = page.locator(sel)
        try:
            n = await loc.count()
        except Exception:
            continue
        for i in range(n - 1, -1, -1):
            item = loc.nth(i)
            try:
                if await item.is_visible() and await item.is_enabled():
                    return item, sel
            except Exception:
                pass
    return None, None


async def is_generating(page, site):
    for sel in sel_list(site, "stop") + [GENERIC_STOP]:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(min(n, 5)):
                if await loc.nth(i).is_visible():
                    return True
        except Exception:
            pass
    return False


async def extract(page, site, query, from_len=0):
    """通用识别：在页面文字里找到问题原文，取它后面的文字。"""
    head = query.strip().splitlines()[0][:40]
    try:
        return await page.evaluate(JS_EXTRACT, [sel_list(site, "scroll"), head, from_len])
    except Exception:
        return {"text": "", "method": "failed"}


async def extract_sel(page, site, md=False):
    """按回答选择器取最新一条回答。text 已去掉排除部分；raw 是容器里的全部文字（含思考过程）。"""
    try:
        return await page.evaluate(JS_EXTRACT_SEL, [sel_list(site, "response"), sel_list(site, "exclude"), md])
    except Exception:
        return {"found": False, "count": 0, "text": "", "raw": "", "md": None, "sel": None}


def is_part_of(t, query):
    """取到的文字只是问题本身的一部分（比如问题还在输入框里），不算回答。"""
    t = re.sub(r"\s+", "", t or "")
    return len(t) < 4 or t in re.sub(r"\s+", "", query or "")


def is_echo(raw, query):
    """选择器匹配到的是用户自己发的那条消息，而不是回答。"""
    r = (raw or "").strip()
    return bool(r) and query.strip().startswith(r[:40])


async def send_text(page, site, box, text):
    """输入并发送。返回 (发送时刻, 发送方式)。"""
    try:
        await box.click(timeout=10000)
    except Exception:
        if site.get("dismiss"):
            await page.evaluate(JS_DISMISS, site["dismiss"])
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.8)
        try:
            await box.click(timeout=5000)
        except Exception:
            raise Attention("blocked", "输入框被弹窗挡住了（可能是登录框或公告）。请在浏览器窗口里关掉弹窗或登录，再点继续")
    tag = (await box.evaluate("e => e.tagName")).lower()
    if tag in ("textarea", "input"):
        await box.fill(text)
    else:
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
        if site.get("typing") == "keys":
            # 有的产品会把一次性粘贴进来的文字当成脚本操作，逐字输入更像真人
            await page.keyboard.type(text, delay=random.uniform(25, 60))
        else:
            await page.keyboard.insert_text(text)
    await asyncio.sleep(0.6)
    before = await page.evaluate(JS_SIGNATURE)
    send_cands = sel_list(site, "send") + [GENERIC_SEND]
    how = "回车"
    if site.get("send_selector"):
        await page.locator(site["send_selector"]).last.click()
        how = "点按钮 " + site["send_selector"]
    elif site.get("submit") == "button":
        btn, sel = await first_visible(page, send_cands)
        if btn:
            await btn.click()
            how = "点按钮 " + sel
        else:
            await page.keyboard.press("Enter")
            how = "回车（没找到发送按钮）"
    else:
        await page.keyboard.press("Enter")
    t_sent = time.monotonic()
    # 确认真的发出去了：问题还留在输入框里就没发出去（比如手机网页里回车只是换行）
    head = text.strip()[:12]
    if await still_in_box(page, box, head):
        btn, sel = await first_visible(page, send_cands)
        if btn:
            await btn.click()
            t_sent = time.monotonic()
            how += f"，没发出去，改点按钮 {sel}"
        if await still_in_box(page, box, head):
            await page.keyboard.press("ControlOrMeta+Enter")
            t_sent = time.monotonic()
            how += "，再试 Ctrl/⌘+回车"
            if await still_in_box(page, box, head):
                raise RuntimeError("发送失败：问题还留在输入框里没发出去。到“产品 → 设置”把发送方式改成“点发送按钮”，或填发送按钮选择器")
    return t_sent, how


async def still_in_box(page, box, head, wait=1.5):
    """发送后等一会儿，问题文字是否还在输入框里。输入框被页面替换掉了视为已发送。"""
    end = time.monotonic() + wait
    while True:
        await asyncio.sleep(0.3)
        try:
            v = await box.evaluate("e => (e.value !== undefined && e.tagName !== 'DIV' ? e.value : e.innerText) || ''", timeout=2000)
        except Exception:
            return False
        if head not in v:
            return False
        if time.monotonic() > end:
            return True


def residual(text, pre_tail):
    """去掉发送前就已存在的文字（输入框提示、免责声明、按钮文字等），剩下的才算回答。"""
    t = text or ""
    for tok in sorted({x for x in re.split(r"\s+", pre_tail or "") if 0 < len(x) <= 24}, key=len, reverse=True):
        t = t.replace(tok, "")
    for w in UI_TAIL_WORDS:
        t = t.replace(w, "")
    return re.sub(r"[\s■□●○…·]+", "", t)


STOP_TRUST = 45     # 页面这么多秒没有任何变化，即使“停止生成”按钮还在也视为结束（防止停止按钮选择器写错导致每条都等到超时）


async def wait_answer(page, site, query, t0, w, engine, base_len=0, pre_tail="", sel_base=None, diag=None):
    """等回答结束。sel_base 是发送前按回答选择器取到的状态，为 None 表示只用通用识别。
    首次输出时间总是按页面上出现新文字算（含思考过程）；正文开始时间按回答选择器取到正文算。
    回答结束时选择器仍没匹配到新回答，就退回通用识别（fallback）。
    返回 dict：status / ttft / answer_at / total（秒）/ footer（清理回答末尾页脚用）/ by_selector / fallback。"""
    by_selector = sel_base is not None
    fallback = sel_seen = False
    last_sig = await page.evaluate(JS_SIGNATURE)
    last_change = time.monotonic()
    ttft = answer_at = None
    baseline, baseline_at = None, None
    checked_block = False

    def result(status, total):
        nonlocal by_selector, fallback
        if by_selector and not sel_seen:
            by_selector, fallback = False, True
        return {"status": status, "ttft": ttft, "answer_at": answer_at, "total": total,
                "footer": "" if by_selector else pre_tail, "by_selector": by_selector, "fallback": fallback}

    while True:
        await asyncio.sleep(0.3 if ttft is None else 0.5)
        now = time.monotonic()
        sig = await page.evaluate(JS_SIGNATURE)
        if sig != last_sig:
            last_sig, last_change = sig, now
        elapsed = now - t0
        if diag is not None and not diag.get("stop_seen") and ttft is not None and await is_generating(page, site):
            diag["stop_seen"] = True
        if ttft is None:
            got = await extract(page, site, query, base_len)
            if got.get("found") and not is_part_of(got.get("text"), query):
                t = got.get("text") or ""
                if baseline is None:
                    baseline, baseline_at = t, now
                if len(residual(t, pre_tail)) >= 2 or len(t) > len(baseline) + 20:
                    ttft = now - t0       # 出现了发送前没有的文字，或文字在持续增长
                elif len(t) > 30 and now - baseline_at > 15:
                    ttft = baseline_at - t0   # 兜底：回答内容恰好和之前页面上的文字重复
        if by_selector and answer_at is None:
            got = await extract_sel(page, site)
            new = got["count"] > sel_base["count"] or (got["count"] and got["raw"] != sel_base["raw"])
            if new and got["raw"] and not is_echo(got["raw"], query):
                sel_seen = True
                if ttft is None:
                    ttft = now - t0
                if got["text"]:
                    answer_at = now - t0
        if ttft is None and elapsed > 30 and not checked_block:
            checked_block = True
            await engine.check_verify(page)
        if elapsed > float(w["max"]):
            return result("timeout" if ttft is not None else "no_answer", elapsed)
        quiet = now - last_change
        if (ttft is not None and elapsed >= float(w["min"]) and quiet >= float(w["stable"])
                and (quiet >= STOP_TRUST or not await is_generating(page, site))):
            if by_selector and sel_seen:
                tail = (await extract_sel(page, site))["raw"][-60:]
            else:
                tail = ((await extract(page, site, query, base_len)).get("text") or "")[-60:]
            if find_pattern(tail, GENERATING_HINTS):
                continue          # 末尾还显示“思考中/搜索中”，说明只是停顿
            await asyncio.sleep(1.5)  # 再确认一次，防止恰好卡在两段输出之间
            if await page.evaluate(JS_SIGNATURE) != last_sig:
                last_sig, last_change = await page.evaluate(JS_SIGNATURE), time.monotonic()
                continue
            return result("ok", max(last_change - t0, ttft))


async def long_screenshot(page, site, base_path, shot, diag=None):
    how = await page.evaluate(JS_MARK_SCROLLER, [sel_list(site, "scroll"), sel_list(site, "response")])
    if diag is not None:
        diag["scroll"] = how or "none"
    if not how:
        raw = await page.screenshot()
        return await asyncio.to_thread(lambda: save_image(Image.open(io.BytesIO(raw)).convert("RGB"), base_path, shot))
    await page.evaluate(JS_HIDE_FLOATING, True)
    try:
        info = await page.evaluate(JS_SCROLL_INFO)
        vp = page.viewport_size
        clip = {"x": max(0, info["x"]), "y": max(0, info["y"]),
                "width": min(info["w"], vp["width"] - max(0, info["x"])),
                "height": min(info["ch"], vp["height"] - max(0, info["y"]))}
        step = max(100, clip["height"] - 40)
        lo, hi = info["min"], info["max"]
        pieces = []
        pos = lo
        for _ in range(80):
            actual = await page.evaluate(JS_SCROLL_TO, pos)
            await asyncio.sleep(0.35)
            pieces.append((actual - lo, await page.screenshot(clip=clip)))
            if actual >= hi - 1:
                break
            pos = actual + step
            hi = (await page.evaluate(JS_SCROLL_INFO))["max"]
        await page.evaluate(JS_SCROLL_TO, hi)
    finally:
        await page.evaluate(JS_HIDE_FLOATING, False)
    return await asyncio.to_thread(stitch, pieces, clip["width"], clip["height"], base_path, shot)


def stitch(pieces, clip_w, clip_h, base_path, shot):
    """拼接和压缩图片比较耗 CPU，放到线程里做，不影响其他产品的计时。"""
    imgs = [(off, Image.open(io.BytesIO(b)).convert("RGB")) for off, b in pieces]
    scale = imgs[0][1].width / clip_w
    canvas = Image.new("RGB", (imgs[0][1].width, int((imgs[-1][0] + clip_h) * scale)), "white")
    for off, im in imgs:
        canvas.paste(im, (0, int(off * scale)))
    return save_image(canvas, base_path, shot)


def save_image(img, base_path, shot):
    if shot.get("format", "jpg") == "jpg" and img.height < 65000:
        p = base_path.with_suffix(".jpg")
        img.save(p, quality=int(shot.get("quality", 85)), optimize=True)
    else:
        p = base_path.with_suffix(".png")
        img.save(p, optimize=True)
    return p
