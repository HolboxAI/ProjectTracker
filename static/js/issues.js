/* All-issues view: a flat, sortable-agnostic table for scanning and filtering. */

import {
  el, typeMark, priorityMark, avatar, statusPill, relativeTime, plural,
  pointsBadge, keyEl,
} from './ui.js';

const COLUMNS = [
  { label: '', className: '' },
  { label: 'Key', className: '' },
  { label: 'Summary', className: '' },
  { label: 'Status', className: '' },
  { label: 'Priority', className: 'col-optional' },
  { label: 'Assignee', className: '' },
  { label: 'Pts', className: 'col-optional' },
  { label: 'Updated', className: 'col-optional' },
];

export function render(host, state, actions) {
  const issues = state.issues;

  host.appendChild(el('div', { class: 'view-head' }, [
    el('h2', { text: 'All issues' }),
    el('span', {
      class: 'mono',
      text: `${issues.length} ${plural(issues.length, 'issue')} · of ${state.project ? state.project.total_issues : 0} in this project`,
    }),
  ]));

  if (!issues.length) {
    host.appendChild(el('div', { class: 'empty' }, [
      el('div', { class: 'empty-title', text: 'No issues match' }),
      el('p', { text: 'Adjust the filters in the toolbar, or clear them to see everything.' }),
    ]));
    return;
  }

  const table = el('div', { class: 'table' });
  table.appendChild(el('div', { class: 'table-head' }, COLUMNS.map((column) => el('span', {
    class: column.className,
    text: column.label,
  }))));

  issues.forEach((issue) => {
    const cells = [
      { class: '', node: typeMark(issue.type) },
      { class: '', node: keyEl(issue) },
      { class: '', node: el('span', { class: 'row-title', text: issue.title }) },
      { class: '', node: statusPill(issue.status) },
      { class: 'col-optional', node: priorityMark(issue.priority) },
      { class: '', node: el('span', { class: 'row-owner' }, [
        avatar(issue.assignee, { empty: !issue.assignee }),
        el('span', { class: 'row-owner-name', text: issue.assignee || 'Unassigned' }),
      ]) },
      { class: 'col-optional', node: pointsBadge(issue.story_points) || el('span', { class: 'row-dim', text: '—' }) },
      { class: 'col-optional', node: el('span', { class: 'row-dim', text: relativeTime(issue.updated_at) }) },
    ];

    const row = el('button', {
      class: 'table-row',
      attrs: { type: 'button' },
      on: { click: () => actions.openIssue(issue.id) },
    }, cells.map((cell) => {
      const wrapper = el('span', { class: cell.class });
      wrapper.style.display = 'flex';
      wrapper.style.alignItems = 'center';
      wrapper.style.minWidth = '0';
      if (cell.node) wrapper.appendChild(cell.node);
      return wrapper;
    }));

    table.appendChild(row);
  });

  host.appendChild(table);
}
