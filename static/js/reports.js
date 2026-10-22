/* Reports view: counts and points by status, priority, type and person.

   Bars are plain blocks rather than a chart library -- the numbers are what
   matter and the widths are honest ratios of the maximum. */

import {
  el, keyEl, typeMark, statusColor, titleCase, relativeTime, plural,
  STATUS_META, PRIORITY_META, TYPE_META,
} from './ui.js';

export function render(host, state, actions) {
  const stats = state.stats;
  if (!stats) {
    host.appendChild(el('p', { class: 'skeleton', text: 'Loading report…' }));
    return;
  }

  const totals = stats.totals;
  const wrap = el('div', { class: 'reports' });

  wrap.appendChild(el('div', { class: 'view-head' }, [
    el('h2', { text: 'Reports' }),
    el('span', { class: 'mono', text: `${stats.project} · ${totals.total} ${plural(totals.total, 'issue')}` }),
  ]));

  wrap.appendChild(el('div', { class: 'stat-grid' }, [
    stat(totals.total, 'Issues'),
    stat(`${totals.done}`, 'Completed'),
    stat(`${totals.points}`, 'Story points'),
    stat(`${totals.done_points}`, 'Points completed'),
    stat(totals.unassigned, 'Unassigned'),
    stat(totals.backlog, 'In backlog'),
  ]));

  const grid = el('div', { class: 'report-grid' });
  grid.appendChild(barPanel('By status', stats.by_status, STATUS_META, statusColor));
  grid.appendChild(barPanel('By priority', stats.by_priority, PRIORITY_META, (key) => `var(--priority-${key})`));
  grid.appendChild(barPanel('By type', stats.by_type, TYPE_META, (key) => `var(--type-${key})`));
  grid.appendChild(assigneePanel(stats.by_assignee));
  wrap.appendChild(grid);

  wrap.appendChild(sprintPanel(stats.active_sprint));
  wrap.appendChild(recentPanel(stats.recent, actions));

  host.appendChild(wrap);
}

function stat(value, label) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'stat-value', text: String(value) }),
    el('div', { class: 'stat-label', text: label }),
  ]);
}

function barPanel(title, rows, meta, colorOf) {
  const max = Math.max(1, ...rows.map((row) => row.count));
  const panel = el('div', { class: 'panel' }, [
    el('div', { class: 'panel-title', text: title }),
  ]);

  rows.forEach((row) => {
    const entry = meta[row.value] || {};
    const label = entry.label || titleCase(row.value);
    const track = el('div', { class: 'bar-track' });
    const fill = el('div', { class: 'bar-fill' });
    fill.style.width = `${Math.round((row.count / max) * 100)}%`;
    fill.style.background = colorOf(row.value);
    fill.style.minWidth = row.count ? '3px' : '0';
    track.appendChild(fill);

    panel.appendChild(el('div', { class: 'bar-row' }, [
      el('span', { text: label }),
      track,
      el('span', { class: 'bar-value', text: `${row.count}${row.points ? ` · ${row.points}pt` : ''}` }),
    ]));
  });

  return panel;
}

function assigneePanel(rows) {
  const max = Math.max(1, ...rows.map((row) => row.count));
  const panel = el('div', { class: 'panel' }, [
    el('div', { class: 'panel-title', text: 'By assignee' }),
  ]);

  rows.forEach((row) => {
    const track = el('div', { class: 'bar-track' });
    const fill = el('div', { class: 'bar-fill' });
    fill.style.width = `${Math.round((row.count / max) * 100)}%`;
    fill.style.background = 'var(--accent)';
    fill.style.minWidth = row.count ? '3px' : '0';
    track.appendChild(fill);

    panel.appendChild(el('div', { class: 'bar-row' }, [
      el('span', { text: row.name }),
      track,
      el('span', { class: 'bar-value', text: `${row.count} · ${row.open_points}pt` }),
    ]));
  });

  return panel;
}

function sprintPanel(active) {
  const panel = el('div', { class: 'panel' }, [
    el('div', { class: 'panel-title', text: 'Active sprint' }),
  ]);

  if (!active) {
    panel.appendChild(el('p', { class: 'row-dim', text: 'No sprint is running. Start one from the backlog.' }));
    return panel;
  }

  const { sprint, total, done, points, done_points: donePoints } = active;
  const pct = points ? Math.round((donePoints / points) * 100) : 0;

  const track = el('div', { class: 'bar-track' });
  const fill = el('div', { class: 'bar-fill' });
  fill.style.width = `${pct}%`;
  fill.style.background = statusColor('done');
  track.appendChild(fill);

  panel.appendChild(el('p', {}, [
    el('strong', { text: sprint.name }),
    ` — ${done}/${total} ${plural(total, 'issue')} done, ${donePoints}/${points} points (${pct}%)`,
  ]));
  panel.appendChild(track);
  if (sprint.goal) {
    panel.appendChild(el('p', { class: 'row-dim', text: sprint.goal }));
  }
  return panel;
}

function recentPanel(recent, actions) {
  const panel = el('div', { class: 'panel' }, [
    el('div', { class: 'panel-title', text: 'Recently updated' }),
  ]);

  const list = el('ul', { class: 'recent-list' });
  (recent || []).forEach((issue) => {
    list.appendChild(el('li', {}, [
      el('button', {
        class: 'recent-item',
        attrs: { type: 'button' },
        on: { click: () => actions.openIssue(issue.id) },
      }, [
        typeMark(issue.type),
        keyEl(issue),
        el('span', { class: 'recent-title', text: issue.title }),
        el('span', { class: 'spacer' }),
        el('span', { class: 'row-dim', text: relativeTime(issue.updated_at) }),
      ]),
    ]));
  });

  panel.appendChild(list);
  return panel;
}
