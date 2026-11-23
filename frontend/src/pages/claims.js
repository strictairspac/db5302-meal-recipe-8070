import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {claimStatusLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';

const TERMINAL_KEY = 'claim-terminal';
const CLAIM_KEY = 'claim-token';

export function terminalName() {
  let name = sessionStorage.getItem(TERMINAL_KEY);
  if (!name) {
    name = `Web-${Math.random().toString(36).slice(2, 10)}`;
    sessionStorage.setItem(TERMINAL_KEY, name);
  }
  return name;
}

export function heldClaim() {
  try { return JSON.parse(sessionStorage.getItem(CLAIM_KEY) || 'null'); } catch { return null; }
}

function saveHeld(value) {
  if (value) sessionStorage.setItem(CLAIM_KEY, JSON.stringify(value));
  else sessionStorage.removeItem(CLAIM_KEY);
}

function statusBadge(status) {
  return `<span class="status" data-state="${esc(status)}">${esc(claimStatusLabels[status] || status)}</span>`;
}

function heldPanel(held) {
  if (!held) return '';
  return `<aside class="brief"><h2>${esc(held.number)}</h2>
    <dl><dt>状态</dt><dd>${statusBadge(held.status)}</dd><dt>条目</dt><dd>${esc(held.item_count)}</dd>
    <dt>领单终端</dt><dd>${esc(held.terminal)}</dd><dt>租约到期</dt><dd>${esc(date(held.lease_expires_at))}</dd></dl>
    <div class="actions form-actions"><button id="ack-claim" data-id="${held.id}">确认收货</button>
    <button id="resume-claim" class="secondary">断线续领</button></div>
    <p class="note">确认前请保持页面打开。断线后可用同一终端续领；租约到期后可换台终端接着领原批，单号、顺序和内容不变，旧终端的迟到确认会被拒绝。</p></aside>`;
}

function heldItems(held) {
  if (!held?.items?.length) return '';
  return panel('本批备料单', records(['顺序','执行','原料需求摘要','复核人','复核时间'],
    held.items.map(row=>[esc(row.position), `<a href="#evidence/${row.evidence_id}">执行 ${esc(row.run_id)}</a>`,
      esc(row.report?.summary || '—'), esc(row.reviewer || '—'), esc(date(row.reviewed_at))])));
}

export function claimsPage(state) {
  if (state.session.role!=='buyer') return heading(topic.modules[3], '采购员可以分批领走已复核的备料单。');
  const held = heldClaim();
  const history = (state.claims || []).map(row=>[`<a href="#claims/${row.id}">${esc(row.number)}</a>`,
    statusBadge(row.status), esc(row.item_count), esc(row.terminal || '—'), esc(date(row.acknowledged_at))]);
  return `${heading(topic.modules[3], '按复核落账的先后分批领单；一批确认后才发放下一批。',
    `<button id="take-claim">领取下一批</button><span class="note">终端 ${esc(terminalName())}</span>`)}
    <div class="spread"><div>${heldItems(held) || '<p class="empty">当前终端没有待确认的批次。领取后会显示单号、顺序和内容。</p>'}</div>${heldPanel(held)}</div>
    ${panel('领单历史', records(['单号','状态','条目','领单终端','确认时间'], history, '尚无领单记录。'))}`;
}

export function bindClaims(api, update, fail) {
  const take = document.getElementById('take-claim');
  const held = heldClaim();
  take?.addEventListener('click', async()=>{
    take.disabled = true;
    try {
      const batch = await api('/claims', {method:'POST', data:{terminal:terminalName(), ...(held?.claim_token ? {token:held.claim_token} : {})}});
      if (!batch) { fail(new Error('暂无待发的已复核备料单。')); return; }
      if (batch.status==='acknowledged') {
        // The acknowledgement had gone through even though its response was
        // lost; drop the stale token so the next click opens the next batch.
        saveHeld(null);
        fail(new Error('上一批已确认收货，请再次领取下一批。'));
        await update();
        return;
      }
      saveHeld(batch);
      await update();
    } catch (error) { fail(error); }
    finally { take.disabled = false; }
  });
  document.getElementById('resume-claim')?.addEventListener('click', async event=>{
    const button = event.currentTarget;
    button.disabled = true;
    try {
      saveHeld(await api('/claims', {method:'POST', data:{terminal:terminalName(), token:held?.claim_token}}));
      await update();
    } catch (error) { fail(error); }
    finally { button.disabled = false; }
  });
  document.getElementById('ack-claim')?.addEventListener('click', async event=>{
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api(`/claims/${button.dataset.id}/ack`, {method:'POST', data:{token:held?.claim_token}});
      saveHeld(null);
      await update();
    } catch (error) { fail(error); }
    finally { button.disabled = false; }
  });
}
