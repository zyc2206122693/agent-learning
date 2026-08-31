#!/usr/bin/env python3
"""Interactive local web server for the fund dashboard.

Serves fund_dashboard.html with an interactive toolbar, and provides API endpoints
for refreshing data and editing funds.json directly from the browser.
"""

import json
import math
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fund_data import (
    BASE_DIR,
    DASHBOARD_HTML,
    FUNDS_JSON,
    load_funds,
    save_funds,
    read_dashboard,
    save_json,
)
from news_data import get_news
from investment_assistant import ask as ask_investment_assistant
from portfolio_analysis import (
    SETTINGS_PATH,
    TRANSACTIONS_PATH,
    analyze_portfolio,
    load_settings,
    load_transactions,
    simulate_rebalance,
    transaction_summary,
)

UPDATE_SCRIPT = os.path.join(BASE_DIR, "update_fund_dashboard.py")
PORT = 8765
MAX_BODY_SIZE = 256 * 1024
STATIC_FILES = {"chart.js.min.js"}


TOOLBAR_HTML = """
<div id="app-toolbar" style="position: fixed; top: 0; left: 0; right: 0; z-index: 50; background: #1e293b; color: white; padding: 10px 20px; display: flex; gap: 12px; align-items: center; font-family: ui-sans-serif, system-ui, sans-serif; box-shadow: 0 2px 10px rgba(0,0,0,0.2);">
  <span style="font-weight: 700;">🖥️ 基金看板服务器</span>
  <button onclick="refreshData()" style="background: #3b82f6; color: white; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 14px;">🔄 刷新数据</button>
  <button onclick="toggleEdit()" style="background: #10b981; color: white; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 14px;">✏️ 修改持仓</button>
  <button onclick="openStaticFile()" style="background: #64748b; color: white; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 14px;">📁 打开静态文件</button>
  <span id="status" style="margin-left: auto; font-size: 13px; color: #cbd5e1;"></span>
</div>
<div style="height: 52px;"></div>
<div id="edit-modal" style="display: none; position: fixed; top: 70px; left: 50%; transform: translateX(-50%); width: 92%; max-width: 800px; background: white; color: #1f2937; border-radius: 14px; box-shadow: 0 20px 50px rgba(0,0,0,0.35); padding: 24px; z-index: 60; max-height: 80vh; overflow: auto;">
  <h3 style="margin-top: 0; margin-bottom: 8px;">修改持仓配置</h3>
  <p style="font-size: 13px; color: #6b7280; margin-top: 0; margin-bottom: 14px;">修改下方字段后保存，会自动更新 funds.json 并刷新看板。</p>
  <div id="funds-form" style="display: flex; flex-direction: column; gap: 16px;"></div>
  <div style="margin-top: 16px; display: flex; gap: 10px; justify-content: flex-end;">
    <button onclick="toggleEdit()" style="padding: 8px 16px; border: 1px solid #d1d5db; background: #f3f4f6; border-radius: 8px; cursor: pointer;">取消</button>
    <button onclick="saveHoldings()" style="padding: 8px 16px; background: #3b82f6; color: white; border: none; border-radius: 8px; cursor: pointer;">保存并刷新</button>
  </div>
</div>
<script>
  let currentFunds = null;

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[ch]);
  }

  function renderForm(funds) {
    const container = document.getElementById('funds-form');
    container.innerHTML = '';
    funds.forEach((f, idx) => {
      const card = document.createElement('div');
      card.style.cssText = 'border: 1px solid #e5e7eb; border-radius: 10px; padding: 16px; background: #f9fafb;';
      card.innerHTML = `
        <div style="font-weight: 700; margin-bottom: 10px; font-size: 15px;">${escapeHtml(f.name)} <span style="font-weight: normal; color: #6b7280; font-size: 13px;">(${escapeHtml(f.code)})</span></div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
          <div>
            <label style="display: block; font-size: 12px; color: #6b7280; margin-bottom: 4px;">平台</label>
            <input type="text" value="${escapeHtml(f.platform)}" disabled style="width: 100%; padding: 6px 10px; border: 1px solid #e5e7eb; border-radius: 6px; background: #f3f4f6; color: #6b7280; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #6b7280; margin-bottom: 4px;">类型 / 风险</label>
            <input type="text" value="${escapeHtml(f.type)} / ${escapeHtml(f.risk)}" disabled style="width: 100%; padding: 6px 10px; border: 1px solid #e5e7eb; border-radius: 6px; background: #f3f4f6; color: #6b7280; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #374151; margin-bottom: 4px;">持有金额 (元)</label>
            <input type="number" step="0.01" data-idx="${idx}" data-field="amount" value="${f.amount}" style="width: 100%; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #374151; margin-bottom: 4px;">持有份额</label>
            <input type="number" step="0.01" data-idx="${idx}" data-field="shares" value="${f.shares || ''}" style="width: 100%; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #374151; margin-bottom: 4px;">累计投入 (元)</label>
            <input type="number" step="0.01" data-idx="${idx}" data-field="total_invested" value="${f.total_invested || f.amount}" style="width: 100%; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #374151; margin-bottom: 4px;">定投状态</label>
            <input type="text" data-idx="${idx}" data-field="investment_status" value="${f.investment_status}" style="width: 100%; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box;">
          </div>
          <div>
            <label style="display: block; font-size: 12px; color: #374151; margin-bottom: 4px;">配置类别</label>
            <select data-idx="${idx}" data-field="category" style="width: 100%; padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box; background: white;">
              <option value="稳健底仓" ${f.category === '稳健底仓' ? 'selected' : ''}>稳健底仓</option>
              <option value="稳健增强" ${f.category === '稳健增强' ? 'selected' : ''}>稳健增强</option>
              <option value="进攻仓位" ${f.category === '进攻仓位' ? 'selected' : ''}>进攻仓位</option>
              <option value="观察仓位" ${f.category === '观察仓位' ? 'selected' : ''}>观察仓位</option>
              <option value="锁定仓位" ${f.category === '锁定仓位' ? 'selected' : ''}>锁定仓位</option>
            </select>
          </div>
        </div>
      `;
      container.appendChild(card);
    });
  }

  function collectFormData() {
    const inputs = document.querySelectorAll('#funds-form input[data-field], #funds-form select[data-field]');
    const funds = JSON.parse(JSON.stringify(currentFunds));
    inputs.forEach(input => {
      const idx = parseInt(input.dataset.idx);
      const field = input.dataset.field;
      let value = input.value;
      if (field === 'amount' || field === 'total_invested' || field === 'shares') value = parseFloat(value);
      funds[idx][field] = value;
    });
    return { funds: funds };
  }

  async function refreshData() {
    const status = document.getElementById('status');
    status.textContent = '刷新中...';
    try {
      const res = await fetch('/api/refresh', { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        status.textContent = '刷新成功';
        setTimeout(() => location.reload(), 400);
      } else {
        status.textContent = '刷新失败: ' + (data.error || '未知错误');
      }
    } catch (e) {
      status.textContent = '错误: ' + e.message;
    }
  }

  async function toggleEdit() {
    const modal = document.getElementById('edit-modal');
    if (modal.style.display === 'none' || modal.style.display === '') {
      try {
        const res = await fetch('/api/holdings');
        const data = await res.json();
        currentFunds = data.funds;
        renderForm(currentFunds);
        modal.style.display = 'block';
      } catch (e) {
        alert('读取配置失败: ' + e.message);
      }
    } else {
      modal.style.display = 'none';
    }
  }

  async function saveHoldings() {
    const status = document.getElementById('status');
    status.textContent = '保存中...';
    try {
      const payload = collectFormData();
      const res = await fetch('/api/holdings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.success) {
        status.textContent = '保存成功，刷新中...';
        setTimeout(() => location.reload(), 400);
      } else {
        status.textContent = '保存失败: ' + (data.error || '未知错误');
      }
    } catch (e) {
      status.textContent = '错误: ' + e.message;
    }
  }

  function openStaticFile() {
    window.open('/fund_dashboard.html', '_blank');
  }
</script>
"""


NEWS_HTML = """
<section id="news-module" style="max-width: 1100px; margin: 16px auto; background: #ffffff; border-radius: 14px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); padding: 20px 24px; font-family: ui-sans-serif, system-ui, sans-serif; color: #1f2937;">
  <div id="news-head" onclick="toggleNews()" style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 14px; cursor: pointer; user-select: none;">
    <h3 style="margin: 0; font-size: 17px; font-weight: 700;">📰 实时情报（7x24 快讯）<span id="news-arrow" style="font-size: 12px; color: #9ca3af; margin-left: 8px;">▲</span></h3>
    <span id="news-meta" style="font-size: 12px; color: #9ca3af;"></span>
  </div>
  <div id="news-body">
  <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px;">
    <input id="news-q" placeholder="搜索情报..." style="flex: 1; min-width: 180px; padding: 8px 12px; border: 1px solid #e5e7eb; border-radius: 8px; font-size: 14px; background: #f9fafb; outline: none;" />
    <select id="news-cat" style="padding: 8px 10px; border: 1px solid #e5e7eb; border-radius: 8px; font-size: 13px; background: #f9fafb;">
      <option value="">全部分类</option>
      <option>宏观</option>
      <option>行业</option>
      <option>公司</option>
      <option>市场</option>
    </select>
    <select id="news-sent" style="padding: 8px 10px; border: 1px solid #e5e7eb; border-radius: 8px; font-size: 13px; background: #f9fafb;">
      <option value="">全部情绪</option>
      <option>利好</option>
      <option>利空</option>
      <option>中性</option>
    </select>
    <button onclick="loadNews(true)" style="padding: 8px 16px; background: #3b82f6; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 13px;">🔄 刷新</button>
  </div>
  <div id="news-list" style="display: flex; flex-direction: column; gap: 0;"></div>
  </div>
</section>
<script>
  let newsTimer = null;

  function toggleNews() {
    var body = document.getElementById('news-body');
    var arrow = document.getElementById('news-arrow');
    var collapsed = body.style.display === 'none';
    body.style.display = collapsed ? '' : 'none';
    arrow.textContent = collapsed ? '▲' : '▼';
    try { localStorage.setItem('newsCollapsed', collapsed ? '0' : '1'); } catch (e) {}
  }

  function fmtRelTime(timeStr) {
    if (!timeStr) return '';
    const t = new Date(timeStr.replace(/-/g, '/'));
    if (isNaN(t)) return timeStr.slice(5, 16);
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    return timeStr.slice(5, 16);
  }

  function chip(text, bg, color) {
    return '<span style="display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; margin-right:6px; background:' + bg + '; color:' + color + ';">' + text + '</span>';
  }

  function renderNews(data) {
    const box = document.getElementById('news-list');
    document.getElementById('news-meta').textContent = '共 ' + data.total + ' 条（已去重）';
    if (!data.items.length) {
      box.innerHTML = '<div style="padding: 30px; text-align: center; color: #9ca3af; font-size: 14px;">暂无匹配情报</div>';
      return;
    }
    box.innerHTML = data.items.map(function (it) {
      const sentChip = it.sentiment === '利好' ? chip('利好', '#fef2f2', '#dc2626')
        : it.sentiment === '利空' ? chip('利空', '#ecfdf5', '#059669')
        : chip('中性', '#f3f4f6', '#6b7280');
      const catChip = chip(escapeHtml(it.category), '#eff6ff', '#2563eb');
      const relChip = it.related && it.related.length
        ? '<span style="display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; margin-right:6px; background:#f5f3ff; color:#7c3aed;">★ ' + it.related.map(escapeHtml).join('/') + '</span>'
        : '';
      return '<div style="padding: 12px 4px; border-bottom: 1px solid #f3f4f6;">'
        + '<div style="font-size: 12px; color: #9ca3af; margin-bottom: 4px;">' + fmtRelTime(it.time) + ' · ' + it.time.slice(0, 16) + '</div>'
        + '<div style="font-size: 14px; font-weight: 600; margin-bottom: 4px;">' + catChip + sentChip + relChip + escapeHtml(it.title) + '</div>'
        + (it.summary ? '<div style="font-size: 13px; color: #6b7280; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">' + escapeHtml(it.summary) + '</div>' : '')
        + (it.stock.length ? '<div style="font-size: 12px; color: #2563eb; margin-top: 4px;">' + it.stock.map(escapeHtml).join('、') + '</div>' : '')
        + '</div>';
    }).join('');
  }

  async function loadNews(force) {
    const q = document.getElementById('news-q').value;
    const cat = document.getElementById('news-cat').value;
    const sent = document.getElementById('news-sent').value;
    try {
      const res = await fetch('/api/news?q=' + encodeURIComponent(q)
        + '&category=' + encodeURIComponent(cat)
        + '&sentiment=' + encodeURIComponent(sent)
        + '&refresh=' + (force ? '1' : '0'));
      const data = await res.json();
      renderNews(data);
    } catch (e) {
      document.getElementById('news-list').innerHTML = '<div style="padding: 20px; text-align: center; color: #ef4444; font-size: 14px;">情报加载失败: ' + e.message + '</div>';
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    try {
      if (localStorage.getItem('newsCollapsed') === '1') {
        document.getElementById('news-body').style.display = 'none';
        document.getElementById('news-arrow').textContent = '▼';
      }
    } catch (e) {}
    loadNews(false);
    document.getElementById('news-q').addEventListener('input', function () {
      clearTimeout(newsTimer);
      newsTimer = setTimeout(function () { loadNews(false); }, 300);
    });
    document.getElementById('news-cat').addEventListener('change', function () { loadNews(false); });
    document.getElementById('news-sent').addEventListener('change', function () { loadNews(false); });
    // keep the list fresh every 2 minutes while the page is open
    setInterval(function () { loadNews(false); }, 120000);
  });
</script>
"""


PORTFOLIO_LAB_HTML = """
<section id="portfolio-lab" style="max-width:1100px;margin:24px auto;padding:0 2px;font-family:'Avenir Next','PingFang SC',sans-serif;color:#e8edf2;">
  <style>
    #portfolio-lab *{box-sizing:border-box} #portfolio-lab button,#portfolio-lab input,#portfolio-lab select{font:inherit}
    .pl-shell{position:relative;overflow:hidden;border-radius:22px;padding:26px;background:#101820;box-shadow:0 24px 60px rgba(15,23,42,.22)}
    .pl-shell:before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(circle at 88% 8%,rgba(217,164,65,.18),transparent 28%),linear-gradient(115deg,rgba(255,255,255,.035),transparent 35%)}
    .pl-head{position:relative;display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:22px}.pl-kicker{color:#d9a441;font-size:11px;letter-spacing:.22em;text-transform:uppercase}.pl-title{font-family:Georgia,'Songti SC',serif;font-size:27px;margin:5px 0 0}.pl-meta{font-size:12px;color:#91a1b1;text-align:right}
    .pl-grid{position:relative;display:grid;grid-template-columns:1.15fr .85fr;gap:16px}.pl-card{border:1px solid rgba(255,255,255,.09);border-radius:16px;background:rgba(255,255,255,.045);padding:18px}.pl-card h3{font-size:14px;margin:0 0 14px;color:#f8fafc;letter-spacing:.04em}
    .pl-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}.pl-stat{padding:12px;border-radius:12px;background:rgba(0,0,0,.18)}.pl-stat b{display:block;font-family:Georgia,serif;font-size:20px;color:#fff}.pl-stat span{font-size:11px;color:#91a1b1}
    .pl-row{margin:12px 0}.pl-rowtop{display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px}.pl-track{height:7px;border-radius:20px;background:#283541;overflow:hidden}.pl-current{height:100%;border-radius:20px;background:linear-gradient(90deg,#d9a441,#f2cd77)}.pl-target{color:#91a1b1;margin-left:8px}
    .pl-alert{font-size:12px;line-height:1.55;padding:9px 11px;margin-top:8px;border-left:2px solid #d9a441;background:rgba(217,164,65,.08);color:#e7d3aa}.pl-action{display:grid;grid-template-columns:1fr 70px 94px;align-items:center;gap:8px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.07);font-size:12px}.pl-action:last-child{border:0}.pl-buy{color:#ff8d7a}.pl-sell{color:#6ed4ad}.pl-hold{color:#91a1b1}
    .pl-form{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.pl-field label{display:block;font-size:10px;color:#91a1b1;margin:0 0 5px;text-transform:uppercase;letter-spacing:.08em}.pl-field input,.pl-field select{width:100%;color:#edf2f7;background:#18242e;border:1px solid #34424f;border-radius:9px;padding:9px 10px;outline:none}.pl-field input:focus,.pl-field select:focus{border-color:#d9a441}.pl-btn{border:0;border-radius:10px;padding:10px 14px;cursor:pointer;background:#d9a441;color:#101820;font-weight:700}.pl-btn:hover{background:#edbf61}.pl-btn-secondary{background:#263541;color:#e8edf2}.pl-btn-secondary:hover{background:#344653}.pl-wide{grid-column:1/-1}.pl-tx{max-height:205px;overflow:auto;margin-top:12px}.pl-txrow{display:grid;grid-template-columns:78px 60px 1fr 90px;gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:11px;color:#b9c5cf}.pl-empty{padding:25px 0;text-align:center;color:#71808f;font-size:12px}
    @media(max-width:780px){.pl-grid{grid-template-columns:1fr}.pl-stats{grid-template-columns:1fr 1fr}.pl-head{align-items:flex-start;flex-direction:column}.pl-meta{text-align:left}.pl-shell{padding:18px;border-radius:16px}}
  </style>
  <div class="pl-shell">
    <div class="pl-head"><div><div class="pl-kicker">Personal Portfolio Desk</div><h2 class="pl-title">组合控制台</h2></div><div class="pl-meta" id="pl-meta">读取组合数据…</div></div>
    <div class="pl-grid">
      <div>
        <div class="pl-card"><h3>仓位雷达</h3><div class="pl-stats" id="pl-stats"></div><div id="pl-themes"></div><div id="pl-alerts"></div></div>
        <div class="pl-card" style="margin-top:16px"><h3>目标比例与调仓模拟</h3><div id="pl-actions"></div><div class="pl-form" id="pl-targets" style="margin-top:14px"></div><button class="pl-btn pl-wide" style="width:100%;margin-top:10px" onclick="savePortfolioTargets()">保存目标并重新计算</button><div id="pl-target-status" style="font-size:11px;color:#91a1b1;margin-top:8px"></div></div>
      </div>
      <div class="pl-card"><h3>交易流水</h3><div class="pl-stats" id="pl-txstats"></div><form class="pl-form" onsubmit="saveTransaction(event)">
        <div class="pl-field"><label>日期</label><input id="tx-date" type="date" required></div><div class="pl-field"><label>基金代码</label><input id="tx-code" inputmode="numeric" pattern="[0-9]{6}" placeholder="040046" required></div>
        <div class="pl-field"><label>类型</label><select id="tx-type"><option value="buy">买入</option><option value="sell">卖出</option><option value="dividend">现金分红</option></select></div><div class="pl-field"><label>金额（元）</label><input id="tx-amount" type="number" min="0" step="0.01" required></div>
        <div class="pl-field"><label>份额（可选）</label><input id="tx-shares" type="number" min="0" step="0.0001"></div><div class="pl-field"><label>费用（元）</label><input id="tx-fee" type="number" min="0" step="0.01" value="0"></div>
        <div class="pl-field pl-wide"><label>备注</label><input id="tx-note" placeholder="例如：每月定投"></div><button class="pl-btn pl-wide" type="submit">记一笔</button>
      </form><div id="pl-tx-status" style="font-size:11px;color:#91a1b1;margin-top:8px"></div><div class="pl-tx" id="pl-txlist"></div></div>
    </div>
  </div>
</section>
<script>
let portfolioLabData=null;
const plMoney=n=>Number(n||0).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
function plActionLabel(a){return a==='buy'?'买入':a==='sell'?'卖出':'保持'}
function renderPortfolioLab(data){portfolioLabData=data;const a=data.analysis,r=data.rebalance,t=data.transaction_summary,s=data.settings||{};
  document.getElementById('pl-meta').textContent='数据来自本地 JSON · '+new Date().toLocaleString('zh-CN',{hour12:false});
  document.getElementById('pl-stats').innerHTML=`<div class="pl-stat"><b>¥${plMoney(a.total_value)}</b><span>组合市值</span></div><div class="pl-stat"><b>${a.fund_count}</b><span>持仓数量</span></div><div class="pl-stat"><b>${a.high_risk_weight_pct||0}%</b><span>中高风险占比</span></div>`;
  document.getElementById('pl-themes').innerHTML=(a.themes||[]).map(x=>`<div class="pl-row"><div class="pl-rowtop"><span>${escapeHtml(x.theme)}</span><span>${x.weight_pct}%</span></div><div class="pl-track"><div class="pl-current" style="width:${Math.min(x.weight_pct,100)}%"></div></div></div>`).join('');
  document.getElementById('pl-alerts').innerHTML=(a.alerts||[]).map(x=>`<div class="pl-alert">${escapeHtml(x)}</div>`).join('');
  document.getElementById('pl-actions').innerHTML=r.error?`<div class="pl-alert">${escapeHtml(r.error)}</div>`:(r.actions||[]).map(x=>`<div class="pl-action"><span>${escapeHtml(x.theme)} <span class="pl-target">${x.current_pct}% → ${x.target_pct}%</span></span><b class="pl-${x.action}">${plActionLabel(x.action)}</b><span style="text-align:right">${x.difference_amount>0?'+':''}¥${plMoney(x.difference_amount)}</span></div>`).join('');
  const targets=s.theme_targets||{};document.getElementById('pl-targets').innerHTML=Object.entries(targets).map(([k,v])=>`<div class="pl-field"><label>${escapeHtml(k)}（%）</label><input class="pl-target-input" data-theme="${escapeHtml(k)}" type="number" min="0" max="100" step="0.1" value="${v}"></div>`).join('');
  document.getElementById('pl-txstats').innerHTML=`<div class="pl-stat"><b>${t.count}</b><span>已记录</span></div><div class="pl-stat"><b>¥${plMoney(t.net_cash_in)}</b><span>净投入</span></div><div class="pl-stat"><b>¥${plMoney(t.totals.dividend)}</b><span>现金分红</span></div>`;
  document.getElementById('pl-txlist').innerHTML=(data.transactions||[]).slice().reverse().map(x=>`<div class="pl-txrow"><span>${escapeHtml(x.date||'')}</span><span>${escapeHtml(x.type||'')}</span><span>${escapeHtml(x.fund_code||'')} ${escapeHtml(x.note||'')}</span><b style="text-align:right">¥${plMoney(x.amount)}</b></div>`).join('')||'<div class="pl-empty">还没有交易记录</div>';
}
async function loadPortfolioLab(){try{const res=await fetch('/api/portfolio');if(!res.ok)throw new Error('HTTP '+res.status);renderPortfolioLab(await res.json())}catch(e){document.getElementById('pl-meta').textContent='加载失败，请通过本地服务打开：'+e.message}}
async function savePortfolioTargets(){const status=document.getElementById('pl-target-status');const targets={};document.querySelectorAll('.pl-target-input').forEach(i=>targets[i.dataset.theme]=Number(i.value));status.textContent='保存中…';try{const res=await fetch('/api/portfolio-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme_targets:targets})});const data=await res.json();if(!res.ok)throw new Error(data.error||'保存失败');status.textContent='已保存';renderPortfolioLab(data)}catch(e){status.textContent='保存失败：'+e.message}}
async function saveTransaction(event){event.preventDefault();const status=document.getElementById('pl-tx-status');const payload={date:document.getElementById('tx-date').value,fund_code:document.getElementById('tx-code').value,type:document.getElementById('tx-type').value,amount:Number(document.getElementById('tx-amount').value),shares:Number(document.getElementById('tx-shares').value||0),fee:Number(document.getElementById('tx-fee').value||0),note:document.getElementById('tx-note').value};status.textContent='保存中…';try{const res=await fetch('/api/transactions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await res.json();if(!res.ok)throw new Error(data.error||'保存失败');status.textContent='已记录';event.target.reset();document.getElementById('tx-date').value=new Date().toISOString().slice(0,10);document.getElementById('tx-fee').value='0';renderPortfolioLab(data)}catch(e){status.textContent='保存失败：'+e.message}}
document.addEventListener('DOMContentLoaded',()=>{document.getElementById('tx-date').value=new Date().toISOString().slice(0,10);loadPortfolioLab()});
</script>
"""


CHAT_ASSISTANT_HTML = """
<div id="wealth-chat-root" style="font-family:'Avenir Next','PingFang SC',sans-serif">
  <style>
    #wc-launch{position:fixed;right:24px;bottom:24px;z-index:90;width:62px;height:62px;border:0;border-radius:50%;cursor:pointer;color:#10202b;background:linear-gradient(145deg,#f3cf7d,#c89535);box-shadow:0 14px 35px rgba(16,24,32,.28);transition:transform .2s,box-shadow .2s}#wc-launch:hover{transform:translateY(-3px) scale(1.03);box-shadow:0 18px 45px rgba(16,24,32,.35)}#wc-launch .wc-mark{font:700 23px Georgia,serif}#wc-launch .wc-dot{position:absolute;right:5px;top:5px;width:12px;height:12px;border-radius:50%;background:#5dd3a4;border:2px solid #fff}
    #wc-panel{position:fixed;right:24px;bottom:98px;z-index:89;width:min(410px,calc(100vw - 24px));height:min(650px,calc(100vh - 125px));display:none;flex-direction:column;overflow:hidden;border:1px solid rgba(217,164,65,.35);border-radius:22px;background:#f5f1e8;box-shadow:0 30px 80px rgba(11,21,29,.32);transform-origin:bottom right}#wc-panel.wc-open{display:flex;animation:wc-in .22s ease-out}@keyframes wc-in{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}
    .wc-head{position:relative;padding:18px 20px 16px;color:#edf3f5;background:#10202b;overflow:hidden}.wc-head:after{content:"";position:absolute;width:170px;height:170px;right:-70px;top:-110px;border:1px solid rgba(217,164,65,.35);border-radius:50%}.wc-headrow{position:relative;display:flex;align-items:center;gap:11px;z-index:1}.wc-avatar{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:#d9a441;color:#10202b;font:700 17px Georgia,serif}.wc-title{font:700 16px Georgia,'Songti SC',serif}.wc-sub{margin-top:2px;font-size:10px;color:#92a5b2;letter-spacing:.08em}.wc-close{margin-left:auto;border:0;background:transparent;color:#92a5b2;cursor:pointer;font-size:22px}.wc-disclaimer{position:relative;z-index:1;margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08);font-size:10px;color:#91a1ad}
    #wc-messages{flex:1;overflow:auto;padding:18px;background:linear-gradient(180deg,#f7f3eb,#eee8dc)}.wc-msg{display:flex;margin-bottom:13px;animation:wc-msg .18s ease-out}@keyframes wc-msg{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}.wc-msg.user{justify-content:flex-end}.wc-bubble{max-width:92%;padding:12px 14px;border-radius:15px;font-size:13px;line-height:1.68;word-break:break-word}.wc-msg.assistant .wc-bubble{color:#263640;background:#fff;border:1px solid #e3ddd2;border-bottom-left-radius:4px;box-shadow:0 5px 15px rgba(31,41,55,.05)}.wc-msg.user .wc-bubble{max-width:84%;white-space:pre-wrap;color:#f7f4ec;background:#17303e;border-bottom-right-radius:4px}.wc-bubble h2,.wc-bubble h3,.wc-bubble h4{font-family:Georgia,'Songti SC',serif;color:#122936;line-height:1.35;margin:15px 0 8px}.wc-bubble h2{font-size:17px;padding-bottom:7px;border-bottom:1px solid #e8e0d4}.wc-bubble h3{font-size:15px}.wc-bubble h4{font-size:14px}.wc-bubble p{margin:6px 0}.wc-bubble strong{color:#102f3f;font-weight:750}.wc-bubble ul,.wc-bubble ol{margin:7px 0;padding-left:19px}.wc-bubble li{margin:4px 0;padding-left:2px}.wc-bubble hr{border:0;border-top:1px solid #e9e1d5;margin:14px 0}.wc-bubble code{padding:2px 5px;border-radius:5px;background:#f1ece3;color:#815b19;font-size:11px}.wc-table-wrap{overflow-x:auto;margin:10px 0;border:1px solid #e5ddd0;border-radius:10px}.wc-bubble table{width:100%;border-collapse:collapse;font-size:11px;white-space:nowrap}.wc-bubble th{padding:8px 9px;text-align:left;color:#fff;background:#17303e}.wc-bubble td{padding:8px 9px;border-top:1px solid #eee7dc}.wc-bubble tr:nth-child(even) td{background:#faf7f1}.wc-tools{display:inline-flex;flex-wrap:wrap;gap:4px;margin-top:9px;padding:5px 8px;border-radius:8px;color:#7f6331;background:#f5ecd9;font-size:9px;letter-spacing:.03em}.wc-stream-cursor{display:inline-block;width:5px;height:14px;margin-left:2px;vertical-align:-2px;background:#c89535;animation:wc-blink .7s infinite}@keyframes wc-blink{50%{opacity:0}}.wc-thinking span{display:inline-block;width:5px;height:5px;margin:0 2px;border-radius:50%;background:#c89535;animation:wc-pulse 1s infinite}.wc-thinking span:nth-child(2){animation-delay:.15s}.wc-thinking span:nth-child(3){animation-delay:.3s}@keyframes wc-pulse{0%,70%,100%{opacity:.25;transform:translateY(0)}35%{opacity:1;transform:translateY(-3px)}}
    .wc-quick{display:flex;gap:7px;overflow-x:auto;padding:10px 14px 0;background:#f5f1e8}.wc-chip{flex:0 0 auto;border:1px solid #d6c9b2;border-radius:999px;background:transparent;color:#675b49;padding:6px 10px;font-size:11px;cursor:pointer}.wc-chip:hover{border-color:#c89535;color:#8a611d}.wc-compose{display:flex;gap:9px;padding:12px 14px 14px;background:#f5f1e8}.wc-compose textarea{flex:1;resize:none;max-height:92px;min-height:44px;padding:11px 12px;border:1px solid #d8cdbb;border-radius:13px;background:#fffdf8;color:#24333c;outline:none;font:13px/1.5 inherit}.wc-compose textarea:focus{border-color:#c89535;box-shadow:0 0 0 3px rgba(200,149,53,.1)}.wc-send{align-self:flex-end;width:44px;height:44px;border:0;border-radius:12px;background:#d9a441;color:#10202b;cursor:pointer;font-size:18px;font-weight:700}.wc-send:disabled{opacity:.45;cursor:not-allowed}
    @media(max-width:600px){#wc-panel{right:12px;bottom:84px;width:calc(100vw - 24px);height:calc(100vh - 105px)}#wc-launch{right:16px;bottom:16px}}
  </style>
  <button id="wc-launch" onclick="toggleWealthChat()" aria-label="打开理财助手"><span class="wc-mark">财</span><span class="wc-dot"></span></button>
  <aside id="wc-panel" aria-label="理财助手聊天窗口">
    <header class="wc-head"><div class="wc-headrow"><div class="wc-avatar">财</div><div><div class="wc-title">我的理财助手</div><div class="wc-sub">PORTFOLIO COPILOT · LOCAL</div></div><button class="wc-close" onclick="toggleWealthChat()" aria-label="关闭">×</button></div><div class="wc-disclaimer">基于你的本地持仓与工具数据回答 · 只分析，不自动交易</div></header>
    <div id="wc-messages"></div>
    <div class="wc-quick"><button class="wc-chip" onclick="askQuick('我的组合风险怎么样？')">组合风险</button><button class="wc-chip" onclick="askQuick('按目标比例，我应该怎么调仓？')">调仓模拟</button><button class="wc-chip" onclick="askQuick('总结一下我的交易流水')">交易流水</button><button class="wc-chip" onclick="askQuick('最近有哪些值得关注的财经新闻？')">市场动态</button></div>
    <div class="wc-compose"><textarea id="wc-input" rows="1" placeholder="问问持仓、风险、调仓或市场动态…" onkeydown="wealthChatKey(event)"></textarea><button id="wc-send" class="wc-send" onclick="sendWealthChat()">↑</button></div>
  </aside>
</div>
<script>
let wealthChatHistory=[];let wealthChatBusy=false;
function wcEsc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function wcInline(s){return wcEsc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')}
function wcMarkdown(text){const lines=String(text||'').replace(/\\r/g,'').split('\\n');let out='',i=0;while(i<lines.length){const line=lines[i];if(/^\s*\|.*\|\s*$/.test(line)&&i+1<lines.length&&/^\s*\|?[\s:|-]+\|\s*$/.test(lines[i+1])){const heads=line.split('|').slice(1,-1).map(x=>x.trim());i+=2;let rows='';while(i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i])){const cells=lines[i].split('|').slice(1,-1).map(x=>x.trim());rows+='<tr>'+cells.map(x=>'<td>'+wcInline(x)+'</td>').join('')+'</tr>';i++}out+='<div class="wc-table-wrap"><table><thead><tr>'+heads.map(x=>'<th>'+wcInline(x)+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div>';continue}if(/^\s*[-*]\s+/.test(line)){let items='';while(i<lines.length&&/^\s*[-*]\s+/.test(lines[i])){items+='<li>'+wcInline(lines[i].replace(/^\s*[-*]\s+/,''))+'</li>';i++}out+='<ul>'+items+'</ul>';continue}if(/^\s*\d+[.)]\s+/.test(line)){let items='';while(i<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i])){items+='<li>'+wcInline(lines[i].replace(/^\s*\d+[.)]\s+/,''))+'</li>';i++}out+='<ol>'+items+'</ol>';continue}const h=line.match(/^(#{1,4})\s+(.+)/);if(h){const level=Math.min(h[1].length+1,4);out+='<h'+level+'>'+wcInline(h[2])+'</h'+level+'>';i++;continue}if(/^\s*---+\s*$/.test(line)){out+='<hr>';i++;continue}if(line.trim())out+='<p>'+wcInline(line.trim())+'</p>';i++}return out}
function wcLoad(){try{const saved=JSON.parse(localStorage.getItem('wealthChatHistory')||'[]');if(Array.isArray(saved))wealthChatHistory=saved.slice(-20)}catch(e){}if(!wealthChatHistory.length)wealthChatHistory=[{role:'assistant',content:'你好，我可以读取这份看板里的持仓、风险、目标比例、交易流水和财经快讯。\\n\\n你可以直接问：“我的组合有什么风险？”'}];wcRender()}
function wcSave(){try{localStorage.setItem('wealthChatHistory',JSON.stringify(wealthChatHistory.slice(-20)))}catch(e){}}
function wcRender(){const box=document.getElementById('wc-messages');box.innerHTML=wealthChatHistory.map(m=>`<div class="wc-msg ${m.role}"><div class="wc-bubble">${m.role==='assistant'?wcMarkdown(m.content):wcEsc(m.content)}${m.streaming?'<span class="wc-stream-cursor"></span>':''}${m.tools&&m.tools.length?`<div class="wc-tools">数据工具 · ${m.tools.map(wcEsc).join(' · ')}</div>`:''}</div></div>`).join('');box.scrollTop=box.scrollHeight}
function toggleWealthChat(){const panel=document.getElementById('wc-panel');panel.classList.toggle('wc-open');if(panel.classList.contains('wc-open')){wcLoad();setTimeout(()=>document.getElementById('wc-input').focus(),100)}}
function askQuick(text){document.getElementById('wc-input').value=text;sendWealthChat()}
function wealthChatKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendWealthChat()}}
async function sendWealthChat(){if(wealthChatBusy)return;const input=document.getElementById('wc-input');const question=input.value.trim();if(!question)return;const previous=wealthChatHistory.filter(m=>m.role==='user'||m.role==='assistant').slice(-8).map(({role,content})=>({role,content}));wealthChatHistory.push({role:'user',content:question});const answer={role:'assistant',content:'',tools:[],streaming:true};wealthChatHistory.push(answer);input.value='';wcRender();wealthChatBusy=true;document.getElementById('wc-send').disabled=true;
  try{const res=await fetch('/api/chat-stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,history:previous})});if(!res.ok){const err=await res.json();throw new Error(err.error||'请求失败')}if(!res.body)throw new Error('浏览器不支持流式读取');const reader=res.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const part=await reader.read();if(part.done)break;buffer+=decoder.decode(part.value,{stream:true});const lines=buffer.split('\\n');buffer=lines.pop();for(const line of lines){if(!line.trim())continue;const event=JSON.parse(line);if(event.type==='meta')answer.tools=event.tools||[];if(event.type==='delta')answer.content+=event.text||'';if(event.type==='done')answer.streaming=false}wcRender()}answer.streaming=false;if(!answer.content)answer.content='暂时没有生成回答。'}catch(e){answer.streaming=false;answer.content='这次没有连上助手：'+e.message+'\\n请检查本地服务和 API 配置。'}finally{wealthChatBusy=false;document.getElementById('wc-send').disabled=false;wcSave();wcRender();input.focus()}}
document.addEventListener('DOMContentLoaded',wcLoad);
</script>
"""


def portfolio_payload():
    """Return one consistent snapshot for the portfolio console."""
    settings = load_settings()
    transactions = load_transactions()
    return {
        "analysis": analyze_portfolio(settings=settings),
        "rebalance": simulate_rebalance(settings=settings),
        "settings": settings,
        "transactions": transactions,
        "transaction_summary": transaction_summary(transactions),
    }


def inject_toolbar(html_content):
    """Insert local controls and interactive personal-finance modules."""
    content = re.sub(
        r"(<body[^>]*>)",
        lambda match: match.group(1) + TOOLBAR_HTML,
        html_content,
        count=1,
        flags=re.IGNORECASE,
    )
    modules = PORTFOLIO_LAB_HTML + NEWS_HTML + CHAT_ASSISTANT_HTML
    return re.sub(
        r"(</body>)",
        lambda match: modules + match.group(1),
        content,
        count=1,
        flags=re.IGNORECASE,
    )


def run_update_script():
    try:
        result = subprocess.run(
            [sys.executable, UPDATE_SCRIPT],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)


class FundHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, html_content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def send_ndjson_event(self, data):
        payload = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()

    def do_GET(self):
        from urllib.parse import urlparse
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == "/" or path == "/index.html":
            try:
                html_content = inject_toolbar(read_dashboard())
                self.send_html(html_content)
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if path == "/api/holdings":
            try:
                self.send_json(load_funds())
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if path == "/api/news":
            try:
                from urllib.parse import parse_qs
                qs = parse_qs(parsed_url.query)
                res = get_news(
                    query=qs.get("q", [""])[0],
                    category=qs.get("category", [""])[0],
                    sentiment=qs.get("sentiment", [""])[0],
                    refresh=qs.get("refresh", ["0"])[0] == "1",
                )
                self.send_json(res)
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if path == "/api/portfolio":
            try:
                self.send_json(portfolio_payload())
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        if path == "/fund_dashboard.html":
            try:
                self.send_html(read_dashboard())
            except Exception as e:
                self.send_json({"error": str(e)}, status=500)
            return

        # Serve only explicitly approved assets; never map arbitrary URL paths.
        static_name = path.lstrip("/")
        if static_name in STATIC_FILES:
            file_path = os.path.join(BASE_DIR, static_name)
            if os.path.exists(file_path) and os.path.isfile(file_path):
                try:
                    with open(file_path, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    if file_path.endswith(".js"):
                        self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    elif file_path.endswith(".css"):
                        self.send_header("Content-Type", "text/css; charset=utf-8")
                    elif file_path.endswith(".html"):
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                    elif file_path.endswith(".json"):
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                    else:
                        self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self.send_json({"error": str(e)}, status=500)
                return

        self.send_json({"error": "Not found"}, status=404)

    def do_POST(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=400)
            return
        if content_length < 0 or content_length > MAX_BODY_SIZE:
            self.send_json({"error": "Request body too large"}, status=413)
            return
        body = self.rfile.read(content_length).decode("utf-8")

        if path == "/api/refresh":
            success, output = run_update_script()
            self.send_json({"success": success, "output": output})
            return

        if path == "/api/holdings":
            try:
                data = json.loads(body)
                if not isinstance(data.get("funds"), list):
                    raise ValueError("配置缺少 funds 数组")
                for fund in data["funds"]:
                    if not isinstance(fund, dict) or not re.fullmatch(r"\d{6}", str(fund.get("code", ""))):
                        raise ValueError("基金代码必须是 6 位数字")
                    for field in ("amount", "shares", "total_invested"):
                        if field not in fund or fund[field] in (None, ""):
                            continue
                        value = float(fund[field])
                        if not math.isfinite(value) or value < 0:
                            raise ValueError(f"{field} 必须是非负有限数")
                save_funds(data)
                success, output = run_update_script()
                self.send_json({"success": success, "output": output})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)
            return

        if path == "/api/portfolio-settings":
            try:
                data = json.loads(body)
                targets = data.get("theme_targets")
                if not isinstance(targets, dict) or not targets:
                    raise ValueError("theme_targets 必须是非空对象")
                clean = {}
                for theme, value in targets.items():
                    number = float(value)
                    if not str(theme).strip() or not math.isfinite(number) or number < 0 or number > 100:
                        raise ValueError("方向名称不能为空，比例必须在 0 到 100 之间")
                    clean[str(theme).strip()] = round(number, 2)
                if abs(sum(clean.values()) - 100) > 0.01:
                    raise ValueError(f"目标比例合计必须为 100%，当前为 {sum(clean.values()):.2f}%")
                settings = load_settings()
                settings["theme_targets"] = clean
                save_json(SETTINGS_PATH, settings)
                self.send_json(portfolio_payload())
            except Exception as e:
                self.send_json({"error": str(e)}, status=400)
            return

        if path == "/api/transactions":
            try:
                row = json.loads(body)
                if not isinstance(row, dict):
                    raise ValueError("交易记录格式错误")
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row.get("date", ""))):
                    raise ValueError("日期必须是 YYYY-MM-DD")
                code = str(row.get("fund_code", ""))
                known_codes = {str(f.get("code", "")) for f in (load_funds() or {}).get("funds", [])}
                if not re.fullmatch(r"\d{6}", code) or code not in known_codes:
                    raise ValueError("基金代码必须是当前持仓中的 6 位代码")
                tx_type = str(row.get("type", "")).lower()
                if tx_type not in ("buy", "sell", "dividend"):
                    raise ValueError("交易类型必须是 buy、sell 或 dividend")
                for field in ("amount", "shares", "fee"):
                    value = float(row.get(field, 0) or 0)
                    if not math.isfinite(value) or value < 0:
                        raise ValueError(f"{field} 必须是非负有限数")
                    row[field] = round(value, 4)
                row["fund_code"] = code
                row["type"] = tx_type
                row["note"] = str(row.get("note", ""))[:100]
                transactions = load_transactions()
                transactions.append(row)
                save_json(TRANSACTIONS_PATH, {"transactions": transactions})
                self.send_json(portfolio_payload())
            except Exception as e:
                self.send_json({"error": str(e)}, status=400)
            return

        if path == "/api/chat":
            try:
                data = json.loads(body)
                question = str(data.get("question", "")).strip()
                if not question:
                    raise ValueError("问题不能为空")
                if len(question) > 1000:
                    raise ValueError("问题不能超过 1000 个字符")
                raw_history = data.get("history", [])
                if not isinstance(raw_history, list):
                    raise ValueError("history 必须是数组")
                history = []
                for message in raw_history[-8:]:
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    content = message.get("content")
                    if role in ("user", "assistant") and isinstance(content, str):
                        history.append({"role": role, "content": content[:3000]})
                result = ask_investment_assistant(question, history=history, max_tokens=900)
                tool_names = [call.get("name", "") for call in result.get("tool_calls", []) if call.get("name")]
                self.send_json({
                    "answer": result.get("final_text", ""),
                    "tools": tool_names,
                    "steps": result.get("steps", 0),
                })
            except Exception as e:
                self.send_json({"error": str(e)}, status=400)
            return

        if path == "/api/chat-stream":
            try:
                data = json.loads(body)
                question = str(data.get("question", "")).strip()
                if not question:
                    raise ValueError("问题不能为空")
                if len(question) > 1000:
                    raise ValueError("问题不能超过 1000 个字符")
                raw_history = data.get("history", [])
                if not isinstance(raw_history, list):
                    raise ValueError("history 必须是数组")
                history = []
                for message in raw_history[-8:]:
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    content = message.get("content")
                    if role in ("user", "assistant") and isinstance(content, str):
                        history.append({"role": role, "content": content[:3000]})
                result = ask_investment_assistant(question, history=history, max_tokens=900)
                answer = result.get("final_text", "") or "暂时没有生成回答。"
                tool_names = [call.get("name", "") for call in result.get("tool_calls", []) if call.get("name")]
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.send_ndjson_event({"type": "meta", "tools": tool_names, "steps": result.get("steps", 0)})
                for start in range(0, len(answer), 18):
                    self.send_ndjson_event({"type": "delta", "text": answer[start:start + 18]})
                    time.sleep(0.012)
                self.send_ndjson_event({"type": "done"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                if not self.wfile.closed:
                    self.send_json({"error": str(e)}, status=400)
            return

        self.send_json({"error": "Not found"}, status=404)


def main():
    if not os.path.exists(DASHBOARD_HTML):
        print(f"[INFO] {DASHBOARD_HTML} not found, generating initial dashboard...")
        run_update_script()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), FundHandler)
    print(f"[INFO] Fund dashboard server running at http://127.0.0.1:{PORT}")
    print("[INFO] Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
