#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""UI layer for report table filtering and client-side sorting.

The canonical report portal remains in ``run_report_portal.py``. This module
replaces the plate dropdown with a typed search field and makes every visible
result column sortable without changing report generation logic.
"""

from __future__ import annotations

import run_report_portal as portal

implementation = portal.implementation


# Replace the exact-value dropdown with a text field plus browser suggestions.
implementation.HTML = implementation.HTML.replace(
    '<select id="plateFilter"><option value="">Все госномера</option></select>',
    '''<input id="plateFilter" type="text" list="plateSuggestions"
                  placeholder="Введите госномер" autocomplete="off">
           <datalist id="plateSuggestions"></datalist>''',
)

# Inject styles directly before </style>. This is deliberately independent of
# CSS selectors added by other compatibility layers, so a missing anchor cannot
# silently disable sorting icons or the pointer cursor.
SORTING_STYLE_MARKER = ".sort-indicator::before"
SORTING_STYLES = """
    th.sortable { cursor:pointer !important; user-select:none; transition:background-color .12s ease; }
    th.sortable:hover { background:#e7edf6; }
    .sort-indicator { display:inline-block; position:relative; width:8px; height:12px; margin-left:6px; vertical-align:-1px; opacity:.55; }
    .sort-indicator::before { content:''; position:absolute; left:1px; top:1px; border-left:3px solid transparent; border-right:3px solid transparent; border-bottom:4px solid #667085; }
    .sort-indicator::after { content:''; position:absolute; left:1px; bottom:1px; border-left:3px solid transparent; border-right:3px solid transparent; border-top:4px solid #667085; }
    th.sortable.sort-asc .sort-indicator, th.sortable.sort-desc .sort-indicator { opacity:1; }
    th.sortable.sort-asc .sort-indicator::after { display:none; }
    th.sortable.sort-desc .sort-indicator::before { display:none; }
    th.sortable.sort-asc .sort-indicator::before { border-bottom-color:#1663d6; }
    th.sortable.sort-desc .sort-indicator::after { border-top-color:#1663d6; }
    #plateFilter { text-transform:uppercase; }
"""
if SORTING_STYLE_MARKER not in implementation.HTML:
    if "</style>" not in implementation.HTML:
        raise RuntimeError("Не найден закрывающий тег </style> для стилей таблицы")
    implementation.HTML = implementation.HTML.replace(
        "</style>",
        SORTING_STYLES + "\n  </style>",
        1,
    )

# Bind the datalist and sorting state.
implementation.HTML = implementation.HTML.replace(
    "const plateFilter = document.getElementById('plateFilter');\n"
    "const plateFilterCount = document.getElementById('plateFilterCount');",
    "const plateFilter = document.getElementById('plateFilter');\n"
    "const plateSuggestions = document.getElementById('plateSuggestions');\n"
    "const plateFilterCount = document.getElementById('plateFilterCount');",
)
implementation.HTML = implementation.HTML.replace(
    "let tableColumns = [];\nlet tableRows = [];\nlet excelBase64 = '';",
    "let tableColumns = [];\n"
    "let tableRows = [];\n"
    "let sortColumnIndex = -1;\n"
    "let sortDirection = 1;\n"
    "let excelBase64 = '';",
)

# Replace the table renderer installed by run_report_portal with filtering and
# generic sorting for text, numeric, date and date-time values.
old_renderer = '''function drawFilteredTable() {
  const plateIndex = tableColumns.indexOf('Госномер');
  const addressIndex = tableColumns.indexOf('Адрес');
  const mapIndex = tableColumns.indexOf('Карта');
  const visibleIndexes = tableColumns.map((_, index) => index).filter(index => index !== mapIndex);
  const selected = plateFilter.value;
  const rows = selected && plateIndex >= 0 ? tableRows.filter(row => String(row[plateIndex] ?? '') === selected) : tableRows;
  const head = `<thead><tr>${visibleIndexes.map(index => `<th>${esc(tableColumns[index])}</th>`).join('')}</tr></thead>`;
  let previousPlate = null;
  const bodyRows = rows.map(row => {
    const plate = plateIndex >= 0 ? String(row[plateIndex] ?? '') : '';
    const className = plate && plate !== previousPlate ? ' class="plate-start"' : '';
    previousPlate = plate;
    const cells = visibleIndexes.map(index => {
      const value = row[index];
      if (index === addressIndex && mapIndex >= 0 && row[mapIndex]) {
        const label = value || 'Открыть на карте';
        return `<td><a class="map-link" href="${esc(row[mapIndex])}" target="_blank" rel="noopener noreferrer">${esc(label)}</a></td>`;
      }
      return `<td>${esc(value)}</td>`;
    }).join('');
    return `<tr${className}>${cells}</tr>`;
  }).join('');
  table.innerHTML = head + `<tbody>${bodyRows}</tbody>`;
  plateFilterCount.textContent = `Показано: ${rows.length} из ${tableRows.length}`;
}
function renderTable(columns, rows) {
  tableColumns = columns;
  tableRows = rows;
  const plateIndex = columns.indexOf('Госномер');
  if (plateIndex >= 0) {
    const plates = [...new Set(rows.map(row => String(row[plateIndex] ?? '')).filter(Boolean))].sort();
    plateFilter.innerHTML = '<option value="">Все госномера</option>' + plates.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
    plateFilterBox.classList.remove('hidden');
  } else {
    plateFilterBox.classList.add('hidden');
  }
  drawFilteredTable();
}
plateFilter.addEventListener('change', drawFilteredTable);'''

new_renderer = '''function normalizePlate(value) {
  return String(value ?? '').toUpperCase().replace(/[^0-9A-ZА-ЯЁ]/g, '');
}
function sortableValue(value) {
  if (value == null || value === '') return {kind: 0, value: ''};
  if (typeof value === 'number' && Number.isFinite(value)) return {kind: 3, value};
  const text = String(value).trim();
  const numericText = text.replace(/\s/g, '').replace(',', '.');
  if (/^-?\d+(?:\.\d+)?$/.test(numericText)) return {kind: 3, value: Number(numericText)};
  const dateMatch = text.match(/^(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?$/);
  if (dateMatch) {
    const [, day, month, year, hour = '00', minute = '00', second = '00'] = dateMatch;
    return {kind: 2, value: Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second))};
  }
  return {kind: 1, value: text};
}
function compareCells(left, right) {
  const a = sortableValue(left);
  const b = sortableValue(right);
  if (a.kind === 0 && b.kind !== 0) return 1;
  if (b.kind === 0 && a.kind !== 0) return -1;
  if (a.kind === b.kind && (a.kind === 2 || a.kind === 3)) return a.value - b.value;
  return String(a.value).localeCompare(String(b.value), 'ru', {numeric:true, sensitivity:'base'});
}
function drawFilteredTable() {
  const plateIndex = tableColumns.indexOf('Госномер');
  const addressIndex = tableColumns.indexOf('Адрес');
  const mapIndex = tableColumns.indexOf('Карта');
  const visibleIndexes = tableColumns.map((_, index) => index).filter(index => index !== mapIndex);
  const query = normalizePlate(plateFilter.value);
  let rows = query && plateIndex >= 0
    ? tableRows.filter(row => normalizePlate(row[plateIndex]).includes(query))
    : [...tableRows];
  if (sortColumnIndex >= 0) {
    rows.sort((left, right) => compareCells(left[sortColumnIndex], right[sortColumnIndex]) * sortDirection);
  }
  const head = `<thead><tr>${visibleIndexes.map(index => {
    const directionClass = sortColumnIndex === index ? (sortDirection === 1 ? ' sort-asc' : ' sort-desc') : '';
    return `<th class="sortable${directionClass}" data-sort-index="${index}" title="Нажмите для сортировки">${esc(tableColumns[index])}<span class="sort-indicator" aria-hidden="true"></span></th>`;
  }).join('')}</tr></thead>`;
  let previousPlate = null;
  const bodyRows = rows.map(row => {
    const plate = plateIndex >= 0 ? String(row[plateIndex] ?? '') : '';
    const className = plate && plate !== previousPlate ? ' class="plate-start"' : '';
    previousPlate = plate;
    const cells = visibleIndexes.map(index => {
      const value = row[index];
      if (index === addressIndex && mapIndex >= 0 && row[mapIndex]) {
        const label = value || 'Открыть на карте';
        return `<td><a class="map-link" href="${esc(row[mapIndex])}" target="_blank" rel="noopener noreferrer">${esc(label)}</a></td>`;
      }
      return `<td>${esc(value)}</td>`;
    }).join('');
    return `<tr${className}>${cells}</tr>`;
  }).join('');
  table.innerHTML = head + `<tbody>${bodyRows}</tbody>`;
  plateFilterCount.textContent = `Показано: ${rows.length} из ${tableRows.length}`;
}
function renderTable(columns, rows) {
  tableColumns = columns;
  tableRows = rows;
  sortColumnIndex = -1;
  sortDirection = 1;
  plateFilter.value = '';
  const plateIndex = columns.indexOf('Госномер');
  if (plateIndex >= 0) {
    const plates = [...new Set(rows.map(row => String(row[plateIndex] ?? '')).filter(Boolean))]
      .sort((left, right) => left.localeCompare(right, 'ru', {numeric:true, sensitivity:'base'}));
    plateSuggestions.innerHTML = plates.map(value => `<option value="${esc(value)}"></option>`).join('');
    plateFilterBox.classList.remove('hidden');
  } else {
    plateSuggestions.innerHTML = '';
    plateFilterBox.classList.add('hidden');
  }
  drawFilteredTable();
}
function applyPlateFilter() {
  if (!plateFilter.value.trim()) plateFilter.value = '';
  drawFilteredTable();
}
plateFilter.addEventListener('input', applyPlateFilter);
plateFilter.addEventListener('change', applyPlateFilter);
plateFilter.addEventListener('search', applyPlateFilter);
plateFilter.addEventListener('keyup', event => {
  if (event.key === 'Escape') {
    plateFilter.value = '';
    applyPlateFilter();
  }
});
table.addEventListener('click', event => {
  const header = event.target.closest('th[data-sort-index]');
  if (!header) return;
  const index = Number(header.dataset.sortIndex);
  if (!Number.isInteger(index)) return;
  if (sortColumnIndex === index) sortDirection *= -1;
  else {
    sortColumnIndex = index;
    sortDirection = 1;
  }
  drawFilteredTable();
});'''

if old_renderer not in implementation.HTML:
    raise RuntimeError("Не найден ожидаемый блок таблицы для UI-обновления")
implementation.HTML = implementation.HTML.replace(old_renderer, new_renderer)

app = portal.app

__all__ = ["app"]
