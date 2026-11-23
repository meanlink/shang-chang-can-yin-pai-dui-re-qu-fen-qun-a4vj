import {api, state} from '../state.js';
import {badge, empty, header, input, readForm, stamp, taskLabel, text} from '../ui.js';

function spanLine(task) {
  const spans = task.spans || [];
  if (spans.length <= 1) {
    return `<p class="minor">${stamp(task.hour_start)} · ${text(task.anchor)}</p>`;
  }
  const trail = spans.map(span => `${stamp(span.hour_start)} 热区${span.zone_number}`).join(' → ');
  return `<p class="minor">${text(task.anchor)} · ${badge('跨小时接续', 'good')}</p>
    <p class="minor">原单 #${task.id} 轨迹：${text(trail)}（认领与复测记录沿用）</p>`;
}

export async function tasks() {
  const entries = await api(`/tasks?floor=${state.floor}`);
  const user = state.session.user;
  return {
    html: `${header('现场执行', '客流疏导', '将队尾移至指定临时等候线。接单后必须记录复测人数，低于 8 人方可结案。跨小时接续的单沿用同一编号、认领与复测记录。')}
      <div class="dispatch">${Object.keys(taskLabel).map(status => `<section class="lane"><h2>${taskLabel[status]} <span class="count">${entries.filter(task => task.status === status).length}</span></h2>
        ${entries.filter(task => task.status === status).map(task => `<article class="ticket"><div class="section-line"><b class="mono">#${task.id}</b>${badge(`热区 ${task.zone_number}`)}${task.continued ? badge('接续单', 'good') : ''}</div>
        <h3>${text(task.staging_point)}</h3><p>${text(task.instruction)}</p>${spanLine(task)}
        ${status !== 'closed' ? `<p class="minor"><a href="#lanes" data-plan-lane="${task.id}">查看等候线预留</a></p>` : ''}
        ${status === 'active' ? `<p class="minor"><a href="#surges" data-peak-hour="${text((task.spans?.at(-1)?.hour_start || task.hour_start).slice(0, 16))}">查看该小时临时控流</a></p>` : ''}
        ${status === 'assigned' && user.role === 'steward' ? `<button data-claim="${task.id}">认领疏导任务</button>` : ''}
        ${status === 'active' && task.handled_by === user.id ? `<form class="stack" data-close="${task.id}">${input('复测排队人数','cleared_queue','number','min="0" max="300" required')}${input('结案备注','close_note','text','required minlength="4" maxlength="160"')}<button>提交复测并结案</button></form>` : ''}
        ${status === 'closed' ? `<div class="closed"><strong>复测 ${task.cleared_queue} 人</strong><p>${text(task.close_note)} · ${text(task.handler)}</p></div>` : ''}</article>`).join('') || empty('本栏暂无任务。')}</section>`).join('')}</div>`,
    bind(root, action) {
      root.querySelectorAll('[data-plan-lane]').forEach(link => {
        link.onclick = () => { state.bookingTask = Number(link.dataset.planLane); };
      });
      root.querySelectorAll('[data-peak-hour]').forEach(link => {
        link.onclick = () => { state.hour = link.dataset.peakHour; };
      });
      root.querySelectorAll('[data-claim]').forEach(button => button.addEventListener('click', () =>
        action(() => api(`/tasks/${button.dataset.claim}/claim`, {}), '任务已认领', button)));
      root.querySelectorAll('[data-close]').forEach(form => form.addEventListener('submit', event => {
        event.preventDefault();
        const values = readForm(form);
        action(() => api(`/tasks/${form.dataset.close}/close`, {...values, cleared_queue: Number(values.cleared_queue)}), '疏导已结案', form);
      }));
    },
  };
}
