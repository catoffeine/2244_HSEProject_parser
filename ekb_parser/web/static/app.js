const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="ui-token"]').content;
const number = (v) => new Intl.NumberFormat('ru-RU').format(v || 0);
let presets = [], sources = [], selectedOutput = '', job = {}, rows = [], offset = 0, total = 0;
let statusVersion = 0, tableBusy = false, tableVersion = 0, lastSaved = -1, lastOutput = '', searchTimer;
let pendingDocuments = null;
let lastCoverageVersion;
let hasSavedResult = false, deletingResult = false, deleteTimer;
let noticeTimer, lastStatusError;
let activePage, pollTimer, pendingRunConfig, submitting = false, outputs = [];
let filterVersion = 0;
let mappingVersion = 0, mappingEditorVersion = 0, mappingTargets = [], mappingScopes = [], mappingParameters = [];
let mappingSheet, mappingCell, pendingMappingParameter;
let mappingFiltersState = 'idle';
const catalogueFilters = new Map();
let savedCategory, collectionSource;
let readerCategories = [], readerCategory, readerGroup, readerOffset = 0, readerLimit = 200, readerTotal = 0, readerBusy = false;
let readerIndexVersion = 0, readerRowsVersion = 0, readerPartVersion = 0, readerSearchTimer;
let readerStatistics, readerStatsError;
const DELETE_HOLD_MS = 1500;
const ALL_GROUPS = '*';
const PLACEHOLDER_SOURCE = '__placeholder__';

function placeholderSelected() {
  return $('source').value === PLACEHOLDER_SOURCE;
}

function showPage() {
  const links = [...document.querySelectorAll('.rail .nav')];
  const active = links.find(link => link.pathname === window.location.pathname) || links[0];
  activePage = active.dataset.page;
  document.body.dataset.page = activePage;
  $('page-title').textContent = active.dataset.title;
  document.title = `${$('page-title').textContent} · ЭКБ`;
  const placeholder = placeholderSelected();
  const sharedPage = ['sources', 'logs'].includes(activePage);
  $('source-unavailable').hidden = !placeholder;
  $('run-picker').hidden = placeholder || activePage === 'reader';
  $('stop').hidden = activePage === 'reader';
  $('workspace-task').hidden = activePage === 'reader' || !job.running;
  $('connected-sources').hidden = placeholder;
  $('filters-form').hidden = placeholder;
  $('preset-form').hidden = placeholder || collectionSource !== 'yageo';
  $('mapping-panel').hidden = placeholder || collectionSource !== 'yageo';
  $('run-log').hidden = placeholder;
  for (const view of document.querySelectorAll('[data-view]')) {
    view.hidden = view.dataset.view !== activePage || (placeholder && !sharedPage);
  }
  for (const link of links) {
    const selected = link === active;
    link.classList.toggle('active', selected);
    link.querySelector('.nav-dot').hidden = !selected;
    if (selected) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

function openPage() {
  if ($('field-help')?.open) $('field-help').close();
  if ($('mapping-dialog').open) $('mapping-dialog').close();
  if ($('part-dialog').open) $('part-dialog').close();
  readerPartVersion += 1;
  showPage();
  window.scrollTo(0, 0);
  if (activePage === 'components') refreshParts();
  if (activePage === 'sources') refreshSourceTools();
  if (activePage === 'reader') refreshReader();
  refreshStatus();
}

for (const link of document.querySelectorAll('.rail .nav')) {
  link.addEventListener('click', event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (link.pathname !== window.location.pathname) history.pushState(null, '', link.pathname);
    openPage();
  });
}
window.addEventListener('popstate', openPage);

function notice(message, success = false) {
  const element = $('notice');
  clearTimeout(noticeTimer);
  if (!message) {
    element.classList.remove('visible');
    return;
  }
  element.textContent = message;
  element.classList.toggle('success', success);
  element.classList.add('visible');
  noticeTimer = setTimeout(() => element.classList.remove('visible'), success ? 3000 : 5000);
}
const jobNotices = {
  collect: ['Сбор запущен.', 'Сбор завершён.'],
  export: ['Создание выгрузок запущено.', 'Выгрузки готовы.'],
  datasheets: ['Загрузка PDF началась.', 'Загрузка PDF завершена.'],
  coverage: ['Готовим отчёт о заполненности.', 'Отчёт готов.'],
  filters: ['Получаем фильтры каталога.', 'Фильтры получены.'],
  'convert-requirements': ['Обновляем матрицу.', 'Матрица обновлена.'],
};
const jobLabels = {collect: 'Сбор каталога', export: 'Выгрузка', datasheets: 'Загрузка PDF',
  coverage: 'Отчёт о заполненности', filters: 'Фильтры каталога', 'convert-requirements': 'Матрица требований'};
function updateJob(nextJob, launched = false) {
  const previous = job;
  job = nextJob;
  const messages = jobNotices[job.action];
  if (!messages) return;

  const sameJob = job.started_at === previous.started_at;
  if (job.running) {
    if (job.stopping && !previous.stopping) notice('Останавливаем задачу…', true);
    else if (launched || !sameJob) notice(messages[0], true);
  } else if (launched || (sameJob && previous.running)) {
    if (job.stopped) notice('Задача остановлена. Прогресс сохранён.', true);
    else if (job.exit_code === 0) notice(messages[1], true);
    else notice('Задача завершилась с ошибкой. Подробности в журнале.');
  }
}
async function api(path, options = {}) {
  const headers = {'X-UI-Token': token, ...options.headers};
  if (typeof options.body === 'string') headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers});
  let data;
  try { data = await response.json(); } catch { throw new Error('Не удалось прочитать ответ приложения.'); }
  if (!response.ok) throw new Error(data.error || `Ошибка ${response.status}`);
  return data;
}
function stamp() {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,'0')}${String(d.getDate()).padStart(2,'0')}_${String(d.getHours()).padStart(2,'0')}${String(d.getMinutes()).padStart(2,'0')}${String(d.getSeconds()).padStart(2,'0')}`;
}
function newFolder() {
  const category = presets.find(preset => preset.path === $('preset').value);
  const subcategory = $('subcategory').value;
  const key = subcategory === ALL_GROUPS ? category?.path.split('/').at(-1).replace(/\.json$/, '') : subcategory;
  $('output').value = `data/${collectionSource}/runs/${key || 'collection'}_${stamp()}`;
}
function renderPresets(source, selected) {
  const select = $('preset');
  select.replaceChildren();
  for (const preset of presets.filter(p => p.config.source === source)) {
    select.append(new Option(preset.name, preset.path));
  }
  if (!select.options.length) select.append(new Option('Конфигурация источника', '__default__'));
  if (savedCategory?.source === source) select.append(new Option('Настройки сохранённого сбора', '__saved__'));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}
function selectedCategory() {
  const path = $('preset').value;
  if (path === '__saved__') return savedCategory;
  return presets.find(preset => preset.path === path)?.config || sources.find(source => source.key === collectionSource)?.default_config;
}
function renderSubcategories(config, selected) {
  const select = $('subcategory');
  select.replaceChildren();
  const groups = config?.groups || [];
  for (const group of groups) select.append(new Option(group.name, group.key));
  if (groups.length > 1) select.append(new Option('Все подкатегории', ALL_GROUPS));
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}
function editCollection(config) {
  const source = sources.find(s => s.key === config.source);
  if (source) $('page-size').max = source.max_page_size;
  $('page-size').value = config.page_size;
  $('delay').value = config.download_delay;
  $('adapter-options').value = JSON.stringify(config.options || {}, null, 2);
  $('groups').value = JSON.stringify(config.groups, null, 2);
  tags(); estimate();
}
function applySavedConfig(config) {
  const sameGroup = (left, right) => left.key === right.key && JSON.stringify(left.query) === JSON.stringify(right.query);
  const matching = presets.find(preset => {
    if (preset.config.source !== config.source) return false;
    const groups = preset.config.groups;
    if (config.groups.length === 1) return groups.some(group => sameGroup(group, config.groups[0]));
    return groups.length === config.groups.length && groups.every((group, index) => sameGroup(group, config.groups[index]));
  });
  savedCategory = matching ? undefined : config;
  if (collectionSource !== config.source) {
    collectionSource = config.source;
    if (!placeholderSelected()) {
      $('source').value = config.source;
      sessionStorage.setItem('selected-source', config.source);
    }
    showPage();
    drawSourceTools();
  }
  renderPresets(config.source, matching?.path || '__saved__');
  renderSubcategories(matching?.config || config, config.groups.length === 1 ? config.groups[0].key : ALL_GROUPS);
  editCollection(config);
}
function chooseSubcategory() {
  const config = selectedCategory();
  if (!config) return;
  const key = $('subcategory').value;
  const groups = key === ALL_GROUPS ? config.groups : config.groups.filter(group => group.key === key);
  editCollection({...config, groups});
  document.querySelector('[name=mode][value=new]').checked = true;
  newFolder();
}
function tags() {
  $('group-tags').replaceChildren();
  try {
    const groups = JSON.parse($('groups').value);
    for (const group of groups) {
      const tag = document.createElement('span'); tag.className = 'group-tag'; tag.textContent = group.name || group.key; $('group-tags').append(tag);
    }
  } catch {}
}
function estimate() {
  const pages = Number($('max-pages').value), parts = Number($('max-parts').value), size = Number($('page-size').value);
  $('estimate').replaceChildren(); const icon = document.createElement('span'); icon.textContent = '↳';
  $('estimate').append(icon, pages ? `До ${number(Math.min(pages * size, parts))} компонентов за запуск` : `Без лимита страниц · до ${number(parts)} компонентов в папке`);
}
function busyControls() {
  const busy = !!job.running || deletingResult || submitting;
  const hasOutput = hasSavedResult && lastOutput === selectedOutput;
  $('start').disabled = busy || placeholderSelected();
  $('stop').disabled = !job.running || job.stopping || submitting;
  $('stop').textContent = job.stopping ? 'Сохраняем…' : '■ Остановить';
  $('export').disabled = busy || !hasOutput;
  $('datasheets').disabled = busy || !hasOutput || lastSaved <= 0 || pendingDocuments === 0;
  $('max-documents').disabled = busy || !hasOutput;
  $('coverage-report').disabled = busy || !hasOutput;
  $('supplement-import').disabled = busy || !hasOutput || !$('supplement-file').files.length;
  $('supplement-file').disabled = busy || !hasOutput;
  $('convert-requirements').disabled = busy;
  $('fetch-filters').disabled = busy || !$('filter-preset').value || !$('filter-group').value;
  $('save-preset').disabled = busy;
  const choice = selectedMappingCatalogue();
  const canMap = choice && mappingCell?.configured && mappingCell.field.state === 'required';
  const rule = choice && mappingRule(choice.scope, mappingCell?.field.name);
  $('save-mapping').disabled = busy || !canMap;
  $('defer-mapping').disabled = busy || !canMap || !!rule?.deferred;
  $('remove-mapping').disabled = busy || !rule;
  $('fetch-mapping-filters').disabled = busy || !canMap;
  const canDelete = hasOutput && lastSaved >= 0;
  $('result-delete').hidden = !canDelete;
  $('delete-result').disabled = busy || !canDelete;
  $('delete-target').textContent = canDelete ? selectedOutput : '';
  if ($('delete-result').disabled) cancelDeleteHold();
  for (const node of $('collect-form').querySelectorAll('input,select,textarea')) node.disabled = busy;
  $('existing').disabled = busy;
  $('workspace-task').hidden = activePage === 'reader' || !job.running;
  $('workspace-task').textContent = job.stopping ? 'Остановка…' : jobLabels[job.action] || 'Выполняется задача';
}
const coverageReasons = {
  matrix_missing: 'Матрица требований недоступна',
  unmapped: 'Подгруппа не сопоставлена',
  requirements_pending: 'Требования ещё не заполнены',
  no_required_fields: 'Нет обязательных полей',
};
function mountCoverage() {
  if (!$('coverage-percent')) {
    $('group-progress').insertAdjacentHTML('afterend', `
      <div class="coverage-summary">
        <strong id="coverage-percent">—</strong>
        <div>Заполненность сбора<small id="coverage-caption">Ожидание данных</small></div>
      </div>`);
  }
  if (!$('coverage-column')) {
    const body = $('parts-body');
    const documentColumn = body.closest('table').tHead.rows[0].lastElementChild;
    documentColumn.insertAdjacentHTML('beforebegin', '<th id="coverage-column">ЗАПОЛНЕННОСТЬ</th>');
    const emptyCell = body.querySelector('.empty-table');
    if (emptyCell) emptyCell.colSpan = 7;
  }
  if (!$('part-coverage-caption')) {
    $('part-subtitle').insertAdjacentHTML('afterend', `
      <section class="part-coverage" aria-labelledby="part-coverage-title">
        <h3 id="part-coverage-title">Заполненность по требованиям</h3>
        <p id="part-coverage-caption"></p>
        <dl id="part-coverage-fields"></dl>
      </section>`);
  }
}
function coverageLabel(coverage) {
  return coverage?.percent == null ? (coverageReasons[coverage?.state] || 'Нет оценки') : `${number(coverage.percent)} %`;
}
function drawCoverageSummary(coverage, saved) {
  const percent = $('coverage-percent'), summary = $('coverage-caption');
  if (!percent || !summary) return;
  percent.textContent = coverage?.percent == null ? '—' : coverageLabel(coverage);
  const caption = [];
  if (coverage?.assessed) caption.push(`${number(coverage.filled)} из ${number(coverage.required)} полей заполнено`);
  if (coverage?.unassessed) caption.push(`Без оценки: ${number(coverage.unassessed)} из ${number(coverage.total)} компонентов`);
  if (saved && !coverage) caption.push('Сервер не передал оценку. Перезапустите веб-интерфейс.');
  summary.textContent = caption.join(' · ') || 'Нет компонентов для оценки';
  $('components-percent').textContent = percent.textContent;
  $('components-caption').textContent = summary.textContent;
}
function setDownload(id, available, href) {
  const link = $(id);
  link.hidden = !available;
  if (available) link.href = href;
}
function resultFile(name) {
  return `/api/download/${name}?output=${encodeURIComponent(selectedOutput)}`;
}
function showLog(id, text, placeholder) {
  const element = $(id);
  const nearBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 35;
  const content = text || placeholder;
  if (element.textContent !== content) element.textContent = content;
  if (nearBottom) element.scrollTop = element.scrollHeight;
}
function drawStatus(data) {
  const previousRunning = job.running;
  updateJob(data.job);
  const summary = data.summary;
  drawCoverageSummary(data.coverage, summary.unique_components);
  const saved = summary.unique_components;
  $('saved-count').textContent = number(saved);
  $('page-count').textContent = number(summary.groups.reduce((sum, g) => sum + g.pages, 0));
  $('group-count').textContent = number(summary.groups.length);
  const expected = summary.groups.reduce((sum, g) => sum + (g.expected_count || 0), 0);
  const groupSaved = summary.groups.reduce((sum, g) => sum + g.saved, 0);
  const complete = summary.groups.length > 0 && summary.groups.every(g => g.complete);
  const badge = $('job-state');
  let label = 'Готов к запуску', caption = saved ? 'Сохранённый сбор можно продолжить.' : 'Задайте параметры слева, чтобы начать.';
  badge.className = 'badge';
  if (job.running) { label = job.stopping ? 'Остановка…' : jobLabels[job.action] || 'Выполняется задача'; badge.classList.add('running'); caption = job.stopping ? 'Завершаем запросы и сохраняем результат.' : 'Процесс запущен. Прогресс обновляется автоматически.'; }
  else if (job.exit_code !== null && job.exit_code !== undefined && job.exit_code !== 0) { label = 'Ошибка'; badge.classList.add('error'); caption = 'Посмотрите журнал. Зафиксированные страницы сохранены.'; }
  else if (job.stopped) { label = 'Остановлен'; caption = 'Прогресс сохранён. Выберите «Продолжить» для нового запуска.'; }
  else if (complete) { label = 'Группа собрана'; caption = 'Все записи настроенных групп сохранены.'; }
  else if (saved) { label = 'Сохранено'; caption = 'Достигнут лимит запуска или открыт сохранённый результат.'; }
  badge.textContent = label;
  $('status-caption').textContent = caption;
  $('progress-fill').style.width = `${expected ? Math.min(100, groupSaved / expected * 100) : 0}%`;
  $('progress-label').textContent = expected ? `${number(groupSaved)} из ${number(expected)} записей в группах` : 'Количество записей пока неизвестно';
  $('progress-number').textContent = expected ? `${(groupSaved / expected * 100).toFixed(1)}%` : '—';
  $('group-progress').replaceChildren();
  for (const group of summary.groups) {
    const line = document.createElement('div'); line.className = 'group-line';
    const name = document.createElement('span'); name.textContent = group.name;
    const count = document.createElement('span'); count.textContent = `${number(group.saved)} / ${group.expected_count === null ? '—' : number(group.expected_count)} ${group.complete ? '✓' : ''}`;
    line.append(name, count); $('group-progress').append(line);
  }
  drawFiles(data.files);
  hasSavedResult = data.files.some(file => file.name === 'crawl_state.sqlite3');
  if (hasSavedResult && !outputs.includes(selectedOutput)) {
    outputs = [...outputs, selectedOutput].sort();
    drawRunPicker();
  }
  setDownload('coverage-download', data.files.some(file => file.name === 'coverage.jsonl'), resultFile('coverage.jsonl'));
  setDownload('log-download', data.files.some(file => file.name === 'ui.log'), resultFile('ui.log'));
  const docs = data.datasheets || {};
  pendingDocuments = Number.isInteger(docs.pending) ? docs.pending : null;
  let pendingLabel;
  if (!saved) pendingLabel = 'Откройте сохранённый сбор или соберите компоненты.';
  else if (pendingDocuments === null) pendingLabel = 'Счётчик PDF недоступен. Перезапустите веб-интерфейс и обновите страницу.';
  else if (pendingDocuments === 0) pendingLabel = 'Нет PDF к загрузке';
  else pendingLabel = `Ссылок на PDF к загрузке: ${number(pendingDocuments)}`;
  const documentStatus = [pendingLabel];
  if (docs.unique_files) documentStatus.push(`Сохранено PDF: ${number(docs.unique_files)} · ${selectedOutput}/datasheets/`);
  if (docs.failed) documentStatus.push(`Повторных попыток после ошибок: ${number(docs.failed)}`);
  $('document-status').textContent = documentStatus.join('\n');
  if (activePage === 'logs') {
    showLog('log-content', data.logs, selectedOutput ? 'Журнал появится после запуска задачи.' : 'Выберите сохранённый сбор.');
    showLog('tool-log-content', data.tool_logs, 'Здесь появится вывод задач по фильтрам и матрице.');
  }
  const changed = saved !== lastSaved || selectedOutput !== lastOutput || data.coverage_version !== lastCoverageVersion || (previousRunning && !job.running);
  lastSaved = saved; lastOutput = selectedOutput;
  lastCoverageVersion = data.coverage_version;
  busyControls();
  if (changed && activePage === 'components') refreshParts();
  if (previousRunning && !job.running && activePage === 'sources') refreshSourceTools();
}
function drawFiles(files) {
  const shown = files.filter(f => f.name !== 'ui.log');
  if (!shown.length) {
    $('files').innerHTML = '<div class="empty"><p>' + (selectedOutput
      ? 'В этом сборе пока нет файлов.'
      : 'Выберите сохранённый сбор, чтобы открыть его файлы.') + '</p></div>';
    return;
  }
  const labels = {
    'parts.csv': 'Таблица компонентов',
    'parts.jsonl': 'Записи компонентов',
    'articles.txt': 'Список артикулов',
    'documents.jsonl': 'Ссылки на документацию',
    'group_parameters.json': 'Параметры каталога по группам',
    'summary.json': 'Сводка сбора',
    'last_run.json': 'Последний запуск',
    'crawl_state.sqlite3': 'Рабочая база сбора',
    'collection_config.json': 'Конфигурация сбора',
    'datasheet_downloads.jsonl': 'Журнал загрузок PDF',
    'last_datasheet_run.json': 'Последняя загрузка PDF',
    'coverage.jsonl': 'Отчёт о заполненности',
  };
  $('files').replaceChildren();
  for (const file of shown) {
    const a = document.createElement('a'); a.className = 'file-row'; a.href = resultFile(file.name);
    const icon = document.createElement('span'); icon.className = 'file-icon';
    const extension = file.name.split('.').at(-1);
    icon.textContent = extension === 'sqlite3' ? 'SQLite' : extension.toUpperCase();
    icon.setAttribute('aria-hidden', 'true');
    const name = document.createElement('span'); name.className = 'result-file-name';
    const title = document.createElement('strong'); title.textContent = labels[file.name] || file.name;
    name.append(title);
    if (labels[file.name]) {
      const filename = document.createElement('small'); filename.textContent = file.name;
      name.append(filename);
    }
    const size = document.createElement('small'); size.textContent = file.bytes < 1024 ? `${file.bytes} Б` : file.bytes < 1048576 ? `${(file.bytes/1024).toFixed(1)} КБ` : `${(file.bytes/1048576).toFixed(1)} МБ`;
    const arrow = document.createElement('span'); arrow.className = 'download-arrow';
    const download = document.createElement('span'); download.textContent = 'Скачать ';
    arrow.append(download, '↓');
    a.setAttribute('aria-label', 'Скачать ' + file.name);
    a.append(icon, name, size, arrow); $('files').append(a);
  }
}
async function refreshStatus() {
  if (deletingResult) return;
  clearTimeout(pollTimer);
  const version = ++statusVersion;
  const summaryOnly = placeholderSelected() || activePage === 'reader';
  const output = summaryOnly ? '' : selectedOutput;
  try {
    const query = new URLSearchParams({output, view: activePage || ''});
    const data = await api(`/api/status?${query}`);
    if (version !== statusVersion || (!summaryOnly && selectedOutput !== output) || deletingResult) return;
    lastStatusError = undefined;
    if (summaryOnly) {
      const previousRunning = job.running;
      updateJob(data.job);
      busyControls();
      if (activePage === 'logs') showLog('tool-log-content', data.tool_logs, 'Здесь появится вывод задач по фильтрам и матрице.');
      if (previousRunning && !job.running && activePage === 'sources') refreshRequirements();
      if (previousRunning && !job.running && activePage === 'reader') refreshReader();
      return data;
    }
    if (pendingRunConfig === output) {
      pendingRunConfig = undefined;
      if (data.config) {
        applySavedConfig(data.config);
        $('output').value = output;
        document.querySelector('[name=mode][value=resume]').checked = true;
      }
    }
    drawStatus(data);
    return data;
  }
  catch (error) {
    if (version !== statusVersion) return;
    if (error.message !== lastStatusError) notice(error.message);
    lastStatusError = error.message;
  }
  finally {
    if (version === statusVersion && job.running) pollTimer = setTimeout(refreshStatus, 2000);
  }
}
async function refreshParts() {
  const version = ++tableVersion;
  if (placeholderSelected()) return;
  if (!selectedOutput) {
    rows = []; total = 0; tableBusy = false;
    $('table-total').textContent = '0';
    $('table-range').textContent = 'Нет записей';
    $('parts-body').innerHTML = '<tr><td colspan="7" class="empty-table">Выберите сохранённый сбор</td></tr>';
    $('prev').disabled = $('next').disabled = true;
    return;
  }
  tableBusy = true;
  try {
    const data = await api(`/api/parts?output=${encodeURIComponent(selectedOutput)}&offset=${offset}&search=${encodeURIComponent($('search').value)}`);
    if (version !== tableVersion) return;
    rows = data.rows; total = data.total;
    $('table-total').textContent = number(total); $('parts-body').replaceChildren();
    const hasCoverageColumn = !!$('coverage-column');
    if (!rows.length) $('parts-body').innerHTML = `<tr><td colspan="${hasCoverageColumn ? 7 : 6}" class="empty-table">Нет записей для отображения</td></tr>`;
    for (const row of rows) {
      const tr = document.createElement('tr');
      const values = [row.part_number, row.manufacturer, row.series || '—', row.groups.join(', '), `${row.parameters.length} полей`];
      if (hasCoverageColumn) values.push(coverageLabel(row.coverage));
      for (const [j, value] of values.entries()) {
        const td = document.createElement('td');
        if (j === 0) { const button = document.createElement('button'); button.className='part-button'; button.textContent=value; button.onclick=()=>showPart(row); td.append(button); }
        else td.textContent = value;
        tr.append(td);
      }
      const td = document.createElement('td');
      if (row.datasheet_url && /^https?:\/\//i.test(row.datasheet_url)) { const a=document.createElement('a'); a.href=row.datasheet_url; a.target='_blank'; a.rel='noopener noreferrer'; a.textContent='PDF ↗'; td.append(a); } else td.textContent='—';
      tr.append(td); $('parts-body').append(tr);
    }
    $('table-range').textContent = total ? `${number(offset+1)}–${number(offset+rows.length)} из ${number(total)}` : 'Нет записей';
    $('prev').disabled=offset===0; $('next').disabled=offset+25>=total;
  } catch (error) { if (version === tableVersion) notice(error.message); }
  finally { if (version === tableVersion) tableBusy = false; }
}
function drawPartCoverage(coverage) {
  const summary = $('part-coverage-caption'), fields = $('part-coverage-fields');
  if (!summary || !fields) return;
  const caption = [coverageLabel(coverage)];
  if (coverage?.percent != null) caption.push(`${coverage.filled} из ${coverage.required} полей`);
  if (coverage?.group) caption.push(coverage.group[1]);
  summary.textContent = caption.join(' · ');
  fields.replaceChildren();
  const origins = {catalogue: 'Каталог', matrix: 'Матрица требований', supplement: 'Дополненные данные'};
  for (const field of coverage?.fields || []) {
    const name = document.createElement('dt');
    const value = document.createElement('dd');
    name.textContent = field.name;
    value.textContent = field.value == null ? (field.deferred ? 'Дозаполнить позже' : 'Нет данных') : String(field.value);
    if (field.origin) {
      const source = document.createElement('small');
      source.textContent = origins[field.origin] + (field.origin === 'supplement' ? ` · ${field.evidence}` : '');
      value.append(source);
    }
    fields.append(name, value);
  }
}
function showPart(row, localPdf) {
  $('part-title').textContent = row.part_number;
  $('part-subtitle').textContent = [row.manufacturer, row.series, 'ID ' + row.source_id].filter(Boolean).join(' · ');
  drawPartCoverage(row.coverage);
  setDownload('part-pdf', /^https?:\/\//i.test(row.datasheet_url || ''), row.datasheet_url);
  setDownload('part-product', /^https?:\/\//i.test(row.product_url || ''), row.product_url);
  setDownload('part-local-pdf', !!localPdf, localPdf);
  $('part-parameters').replaceChildren();
  for (const parameter of row.parameters) {
    const tr=document.createElement('tr');
    for (const value of [parameter.name, parameter.formatted_value, parameter.value]) {
      const td = document.createElement('td');
      td.textContent = value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
      tr.append(td);
    }
    $('part-parameters').append(tr);
  }
  $('part-dialog').showModal();
  $('part-dialog').scrollTop = $('part-table-wrap').scrollTop = $('part-table-wrap').scrollLeft = 0;
}
function readerCategoryData() {
  return readerCategories.find(category => category.id === readerCategory);
}
function readerGroupData() {
  return readerCategoryData()?.groups.find(group => group.id === readerGroup);
}
function readerQuery() {
  return new URLSearchParams({source: collectionSource, category: readerCategory, group: readerGroup});
}
function readerChartTrack(parts, total) {
  const track = document.createElement('span');
  track.className = 'reader-chart-track';
  track.setAttribute('aria-hidden', 'true');
  if (!total) return track;
  for (const [, value, tone] of parts) {
    if (!value) continue;
    const bar = document.createElement('span');
    bar.className = 'reader-tone reader-tone-' + tone;
    bar.style.width = (100 * value / total) + '%';
    track.append(bar);
  }
  return track;
}
function readerStackedChart(id, parts, empty) {
  const chart = $(id);
  chart.replaceChildren();
  if (!parts) {
    const message = document.createElement('p');
    message.className = 'reader-chart-empty';
    message.textContent = empty;
    chart.append(message);
    return;
  }
  chart.append(readerChartTrack(parts, parts.reduce((total, [, count]) => total + count, 0)));
  const key = document.createElement('ul');
  key.className = 'reader-chart-key';
  for (const [label, value, tone] of parts) {
    const row = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'reader-chart-key-label reader-tone reader-tone-' + tone;
    name.textContent = label;
    const count = document.createElement('strong');
    count.textContent = number(value);
    row.append(name, count);
    key.append(row);
  }
  chart.append(key);
}
function drawReaderVolume(categories, level) {
  const chart = $('reader-volume');
  chart.replaceChildren();
  const rows = categories.flatMap(category => level === 'source'
    ? [{name: category.name, count: category.groups.reduce((sum, group) => sum + group.count, 0), category: category.id}]
    : category.groups.map(group => ({...group, category: category.id})));
  rows.sort((a, b) => b.count - a.count);
  const max = rows[0]?.count || 0;
  for (const row of rows) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    const label = document.createElement('span');
    const count = document.createElement('strong');
    count.textContent = number(row.count);
    label.append(row.name, count);
    button.append(label, readerChartTrack([['Записи', row.count, 'volume']], max));
    button.addEventListener('click', () => {
      readerCategory = row.category;
      $('reader-category').value = row.category;
      if (row.id) readerGroup = row.id;
      readerOffset = 0;
      readerPartVersion += 1;
      chooseReaderCategory();
      refreshReaderParts();
      $('reader-table-wrap').focus({preventScroll: true});
      $('reader-table-wrap').scrollIntoView({block: 'nearest'});
    });
    item.append(button);
    chart.append(item);
  }
}
function drawReaderStatistics() {
  const level = $('reader-stats-level').value;
  const category = readerCategoryData();
  const group = readerGroupData();
  const categories = level === 'source' ? readerCategories : category ? [{
    ...category, groups: level === 'group' ? (group ? [group] : []) : category.groups,
  }] : [];
  const groups = categories.flatMap(item => item.groups);
  const volume = groups.reduce((sum, item) => sum + item.count, 0);
  const stats = {complete: 0, partial: 0, empty: 0, unassessed: 0, filled: 0, required: 0, with_datasheet: 0};
  if (readerStatistics) {
    for (const item of groups) {
      const counts = readerStatistics[item.id];
      if (counts) for (const key of Object.keys(stats)) stats[key] += counts[key];
    }
  }
  $('reader-stats-scope').textContent = level === 'source'
    ? (sources.find(source => source.key === collectionSource)?.name || '')
    : level === 'group' ? (group?.name || 'Подкатегория') : (category?.name || 'Категория');
  $('reader-volume-title').textContent = level === 'source' ? 'Записи по категориям' : level === 'group' ? 'Записи подкатегории' : 'Записи по подкатегориям';
  drawReaderVolume(categories, level);
  const kpis = $('reader-kpis');
  kpis.replaceChildren();
  const percentage = stats.required ? Math.round(1000 * stats.filled / stats.required) / 10 : null;
  for (const [label, value] of [
    ['Записи в подкатегориях', number(volume)],
    ['Заполнено обязательных полей', percentage === null ? '—' : number(percentage) + ' %'],
    ['Без оценки', readerStatistics ? number(stats.unassessed) : '—'],
    ['Со ссылкой на datasheet', readerStatistics ? number(stats.with_datasheet) : '—'],
  ]) {
    const wrapper = document.createElement('div');
    const title = document.createElement('dt');
    const amount = document.createElement('dd');
    title.textContent = label;
    amount.textContent = value;
    wrapper.append(title, amount);
    kpis.append(wrapper);
  }
  const unavailable = readerStatsError || (!volume ? 'В этой выборке пока нет записей.' : 'Подсчитываем заполненность…');
  readerStackedChart('reader-status-chart', readerStatistics && volume ? [
    ['Все обязательные поля', stats.complete, 'complete'],
    ['Заполнено частично', stats.partial, 'partial'],
    ['Ни одного поля', stats.empty, 'empty'],
    ['Без оценки', stats.unassessed, 'unassessed'],
  ] : null, unavailable);
  readerStackedChart('reader-fields-chart', readerStatistics && stats.required ? [
    ['Заполнено', stats.filled, 'complete'],
    ['Требуется заполнить', stats.required - stats.filled, 'empty'],
  ] : null, readerStatistics && volume ? 'В этой выборке нет полей, доступных для оценки.' : unavailable);
  const note = $('reader-stats-message');
  note.hidden = !volume || level === 'group';
  note.textContent = 'Если артикул входит в несколько подкатегорий, он учитывается в каждой.';
}
async function refreshReaderStatistics(version, source) {
  try {
    const data = await api('/api/reader/statistics?' + new URLSearchParams({source}));
    if (version !== readerIndexVersion || activePage !== 'reader' || collectionSource !== source) return;
    readerStatistics = data.groups;
  } catch (error) {
    if (version !== readerIndexVersion || activePage !== 'reader') return;
    readerStatsError = 'Не удалось подсчитать заполненность. ' + error.message;
  }
  drawReaderStatistics();
}
function readerCoverageCell(cell, coverage) {
  if (coverage?.percent == null) {
    cell.textContent = 'Без оценки';
    cell.title = coverageLabel(coverage);
    return;
  }
  const wrapper = document.createElement('div');
  wrapper.className = 'reader-row-coverage';
  const value = document.createElement('strong');
  value.textContent = coverageLabel(coverage);
  const tone = coverage.filled === coverage.required ? 'complete' : coverage.filled ? 'partial' : 'empty';
  const track = readerChartTrack([['Заполнено', coverage.filled, tone]], coverage.required);
  wrapper.title = coverage.filled + ' из ' + coverage.required + ' обязательных полей';
  wrapper.append(track, value);
  cell.append(wrapper);
}
function readerEmpty(message) {
  readerOffset = readerTotal = 0;
  const body = $('reader-rows');
  body.replaceChildren();
  const cell = body.insertRow().insertCell();
  cell.colSpan = 5;
  cell.className = 'empty-table';
  cell.textContent = message;
  $('reader-range').textContent = '';
  $('reader-table-wrap').classList.remove('paginated');
  updateReaderPagination();
}
function updateReaderPagination() {
  const isFirst = readerOffset === 0;
  const isLast = readerOffset + readerLimit >= readerTotal;
  for (const id of ['reader-first', 'reader-prev']) $(id).disabled = isFirst;
  for (const id of ['reader-next', 'reader-last']) $(id).disabled = isLast;
  $('reader-table-wrap').setAttribute('aria-busy', String(readerBusy));
}
function goToReaderPage(offset) {
  if (readerBusy || offset < 0 || offset === readerOffset || offset >= readerTotal) return;
  refreshReaderParts(offset, true);
}
function drawReaderGroups() {
  const list = $('reader-groups');
  list.replaceChildren();
  for (const group of readerCategoryData()?.groups || []) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'reader-group';
    button.setAttribute('aria-pressed', String(readerGroup === group.id));
    const count = document.createElement('span');
    count.textContent = number(group.count);
    button.append(group.name, count);
    button.addEventListener('click', () => {
      if (readerGroup === group.id) return;
      readerGroup = group.id;
      readerOffset = 0;
      readerPartVersion += 1;
      for (const item of list.children) item.setAttribute('aria-pressed', String(item === button));
      drawReaderSummary();
      drawReaderStatistics();
      refreshReaderParts();
    });
    list.append(button);
  }
}
function chooseReaderCategory() {
  const groups = readerCategoryData()?.groups || [];
  if (!groups.some(group => group.id === readerGroup)) {
    readerGroup = groups.reduce((best, group) => !best || group.count > best.count ? group : best, null)?.id;
    readerOffset = 0;
  }
  drawReaderGroups();
  drawReaderSummary();
  drawReaderStatistics();
}
function drawReaderSummary() {
  const group = readerGroupData();
  const available = !!group?.count;
  $('reader-search').disabled = !available;
  $('reader-selection').textContent = group ? group.name + ' · ' + number(group.count) : '';
  $('reader-updated').textContent = group?.updated
    ? 'Обновлено ' + new Intl.DateTimeFormat('ru-RU', {dateStyle: 'medium'}).format(new Date(group.updated)) : '';
  for (const kind of ['csv', 'jsonl', 'sqlite']) {
    setDownload('reader-' + kind, available, '/api/reader/download/' + kind + '?' + readerQuery());
  }
}
async function refreshReader() {
  const version = ++readerIndexVersion;
  readerRowsVersion += 1;
  if (placeholderSelected() || !collectionSource) return;
  const source = collectionSource;
  try {
    const data = await api('/api/reader?' + new URLSearchParams({source}));
    if (version !== readerIndexVersion || activePage !== 'reader' || collectionSource !== source) return;
    readerCategories = data.categories;
    const select = $('reader-category');
    select.replaceChildren();
    for (const category of readerCategories) select.append(new Option(category.name, category.id));
    select.disabled = !readerCategories.length;
    if (!readerCategories.some(category => category.id === readerCategory)) {
      const available = readerCategories.find(category => category.groups.some(group => group.count));
      readerCategory = (available || readerCategories[0])?.id;
    }
    select.value = readerCategory || '';
    const hasData = readerCategories.some(category => category.groups.some(group => group.count));
    readerStatistics = hasData ? undefined : {};
    readerStatsError = undefined;
    setDownload('reader-archive', hasData, '/api/reader/download/zip?' + new URLSearchParams({source}));
    chooseReaderCategory();
    if (hasData) refreshReaderStatistics(version, source);
    await refreshReaderParts();
  } catch (error) {
    if (version !== readerIndexVersion || activePage !== 'reader') return;
    readerBusy = false;
    readerEmpty('Не удалось открыть каталог. ' + error.message);
  }
}
async function refreshReaderParts(targetOffset = readerOffset, preserveScroll = false) {
  const version = ++readerRowsVersion;
  readerPartVersion += 1;
  if (!readerGroupData()?.count) {
    readerBusy = false;
    readerEmpty(readerGroup ? 'В этой подкатегории пока нет сохранённых компонентов.' : 'Сохранённых компонентов пока нет.');
    return;
  }
  const query = readerQuery();
  const search = $('reader-search').value.trim();
  query.set('search', search);
  query.set('offset', targetOffset);
  readerBusy = true;
  updateReaderPagination();
  try {
    const data = await api('/api/reader/parts?' + query);
    if (version !== readerRowsVersion || activePage !== 'reader') return;
    readerOffset = data.offset;
    readerLimit = data.limit;
    readerTotal = data.total;
    if (!data.total) {
      readerEmpty(search ? 'По этому запросу ничего не найдено.' : 'В этой подкатегории пока нет компонентов.');
      return;
    }
    const body = $('reader-rows');
    const table = $('reader-table-wrap');
    const scrollTop = preserveScroll ? table.scrollTop : 0;
    const entries = document.createDocumentFragment();
    for (const row of data.rows) {
      const tr = document.createElement('tr');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'part-button';
      button.textContent = row.part_number;
      button.addEventListener('click', () => openReaderPart(row.source_id));
      tr.insertCell().append(button);
      for (const value of [row.manufacturer, row.series]) {
        tr.insertCell().textContent = value || '—';
      }
      readerCoverageCell(tr.insertCell(), row.coverage);
      tr.insertCell().textContent = row.has_datasheet ? 'Datasheet' : '—';
      tr.addEventListener('click', event => {
        if (!event.target.closest('button')) openReaderPart(row.source_id);
      });
      entries.append(tr);
    }
    body.replaceChildren(entries);
    table.classList.toggle('paginated', readerTotal > readerLimit);
    table.scrollTop = scrollTop;
    $('reader-range').textContent = number(readerOffset + 1) + '–' + number(readerOffset + data.rows.length) + ' из ' + number(data.total);
  } catch (error) {
    if (version !== readerRowsVersion || activePage !== 'reader') return;
    if (preserveScroll) notice(error.message);
    else readerEmpty('Не удалось загрузить компоненты. ' + error.message);
  } finally {
    if (version === readerRowsVersion) {
      readerBusy = false;
      updateReaderPagination();
    }
  }
}
async function openReaderPart(id) {
  if (readerBusy) return;
  const version = ++readerPartVersion;
  const query = readerQuery();
  query.set('id', id);
  try {
    const row = await api('/api/reader/part?' + query);
    if (version !== readerPartVersion || activePage !== 'reader' || $('part-dialog').open) return;
    showPart(row, row.local_datasheet ? '/api/reader/datasheet?' + query : undefined);
  } catch (error) {
    if (version === readerPartVersion && activePage === 'reader') notice(error.message);
  }
}
async function listConfigs() {
  const data = await api('/api/configs');
  presets = data.presets; sources = data.sources; outputs = data.outputs;
  const selectedSource = sessionStorage.getItem('selected-source');
  const selectedPreset = $('preset').value, selectedSubcategory = $('subcategory').value;
  $('source').replaceChildren();
  for (const source of sources) $('source').append(new Option(source.name, source.key));
  $('source').append(new Option('Другой сайт', PLACEHOLDER_SOURCE));
  if ([...$('source').options].some(option => option.value === selectedSource)) $('source').value = selectedSource;
  $('source').disabled = false;
  collectionSource = placeholderSelected() ? collectionSource || sources[0]?.key : $('source').value;
  renderPresets(collectionSource, selectedPreset);
  renderSubcategories(selectedCategory(), selectedSubcategory);
  if (data.errors?.length) notice('Некоторые конфигурации не загружены: ' + data.errors.map(e => e.path).join(', '));
  drawRunPicker();
  drawSourceTools();
  showPage();
}
function drawRunPicker() {
  const select = $('existing');
  select.replaceChildren(new Option('Выбрать сбор…', ''));
  for (const output of outputs) select.append(new Option(output, output));
  if (outputs.includes(selectedOutput)) select.value = selectedOutput;
}
function selectRun(output, loadConfig = false) {
  selectedOutput = output;
  pendingRunConfig = loadConfig && output ? output : undefined;
  sessionStorage.setItem('selected-output', output);
  offset = 0;
  lastSaved = -1;
  lastOutput = null;
  tableVersion += 1;
  if (activePage === 'components') $('parts-body').replaceChildren();
  drawRunPicker();
  busyControls();
}
function drawSourceTools() {
  const names = $('source-list');
  names.replaceChildren();
  for (const source of sources) {
    const item = document.createElement('li'), link = document.createElement('a');
    link.href = source.website;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = source.name;
    item.append(link);
    names.append(item);
  }
  const select = $('filter-preset'), current = select.value;
  select.replaceChildren();
  for (const preset of presets.filter(preset => preset.config.source === collectionSource)) {
    select.append(new Option(preset.name, preset.path));
  }
  if (presets.some(preset => preset.path === current)) select.value = current;
  drawFilterGroups();
  drawPresetCategories();
}
function drawPresetCategories(preferred = $('preset-category').value) {
  const select = $('preset-category');
  select.replaceChildren();
  for (const preset of presets) {
    if (preset.config.source === 'yageo' && /^configs\/yageo\/[^/]+\.json$/.test(preset.path)) {
      select.append(new Option(preset.name, preset.path));
    }
  }
  select.append(new Option('Создать новую категорию…', 'new'));
  if (preferred && Array.from(select.options).some(option => option.value === preferred)) select.value = preferred;
  updatePresetCategory();
}
function updatePresetCategory() {
  const isNew = $('preset-category').value === 'new';
  $('preset-new-category').hidden = !isNew;
  for (const field of $('preset-new-category').querySelectorAll('input')) field.disabled = !isNew;
  $('save-preset').textContent = isNew ? 'Создать категорию и подкатегорию' : 'Добавить подкатегорию';
}
function drawFilterGroups() {
  const preset = presets.find(candidate => candidate.path === $('filter-preset').value);
  const select = $('filter-group'), current = select.value;
  select.replaceChildren();
  for (const group of preset?.config.groups || []) select.append(new Option(group.name, group.key));
  if (preset?.config.groups.some(group => group.key === current)) select.value = current;
}
async function refreshFilters() {
  const version = ++filterVersion;
  const preset = $('filter-preset').value, group = $('filter-group').value;
  const list = $('filter-list');
  list.replaceChildren();
  $('filters-download').hidden = true;
  if (placeholderSelected() || !preset || !group) return;
  const query = new URLSearchParams({preset, group});
  try {
    const metadata = await api(`/api/filters?${query}`);
    if (version !== filterVersion) return;
    catalogueFilters.set(JSON.stringify([preset, group]), metadata);
    setDownload('filters-download', metadata.available, `/api/filters?${query}&download=1`);
    if (!metadata.available) return;
    for (const field of metadata.fields) {
      const item = document.createElement('li');
      const examples = field.examples.filter(value => value != null).join(', ');
      if (Number.isSafeInteger(field.id) && field.id > 0 && collectionSource === 'yageo') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'text-button';
        button.textContent = `${field.name} · ID ${field.id}`;
        button.addEventListener('click', () => {
          pendingMappingParameter = {preset, group, field};
          $('mapping-panel').scrollIntoView({block: 'start'});
          $('mapping-tabs').querySelector('[aria-selected="true"]')?.focus({preventScroll: true});
          notice('Выберите обязательную ячейку для этого параметра.', true);
        });
        item.append(button, ` · ${number(field.count)}${examples ? ` · ${examples}` : ''}`);
      } else {
        item.textContent = `${field.name} · ${number(field.count)}${examples ? ` · ${examples}` : ''}`;
      }
      list.append(item);
    }
  } catch (error) {
    if (version === filterVersion) notice(error.message);
  }
}
async function refreshSourceTools() {
  const choice = selectedMappingCatalogue();
  const tasks = [refreshCatalogueTools(), refreshRequirements()];
  if (choice && mappingCell.configured && mappingCell.field.state === 'required') {
    tasks.push(loadMappingParameters(choice, {refresh: true}));
  }
  await Promise.all(tasks);
}
function refreshCatalogueTools() {
  return Promise.all([refreshFilters(), refreshMappings()]);
}
function showMappingParameter(fillName = false) {
  const field = mappingParameters.find(field => field.id === Number($('mapping-parameter').value));
  if (fillName) $('mapping-name').value = field?.name || '';
  for (const button of $('mapping-filter-list').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(Number(button.dataset.parameterId) === field?.id));
  }
  const example = $('mapping-parameter-example');
  example.textContent = (field?.examples || []).filter(value => value != null).join(' · ');
  example.hidden = !example.textContent;
}
function drawMappingParameters() {
  const search = $('mapping-filter-search');
  const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const parameters = mappingParameters.filter(field => {
    const label = `${field.name} id ${field.id}`.toLowerCase();
    return terms.every(term => label.includes(term));
  });
  const list = $('mapping-filter-list');
  list.replaceChildren();
  search.disabled = !mappingParameters.length;
  $('fetch-mapping-filters').hidden = !['missing', 'error'].includes(mappingFiltersState);
  const messages = {
    idle: 'Выберите подкатегорию, чтобы увидеть её параметры.',
    loading: 'Загрузка параметров…',
    missing: 'Фильтры этой подкатегории ещё не загружены.',
    error: 'Не удалось загрузить параметры.',
  };
  $('mapping-filter-status').textContent = mappingFiltersState === 'ready'
    ? (parameters.length ? `Параметров: ${number(parameters.length)}` : (terms.length ? 'Ничего не найдено.' : 'Нет параметров с ID.'))
    : messages[mappingFiltersState];
  for (const field of parameters) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.parameterId = field.id;
    const id = document.createElement('span');
    id.className = 'mapping-filter-id';
    id.textContent = `ID ${field.id}`;
    button.append(field.name, id);
    const examples = (field.examples || []).filter(value => value != null);
    if (examples.length) {
      const values = document.createElement('small');
      values.textContent = examples.join(' · ');
      button.append(values);
    }
    button.addEventListener('click', () => {
      $('mapping-parameter').value = field.id;
      $('mapping-name').value = field.name;
      showMappingParameter();
    });
    item.append(button);
    list.append(item);
  }
  showMappingParameter();
}
function applyMappingParameters(metadata) {
  mappingParameters = (metadata.fields || []).filter(field => Number.isSafeInteger(field.id) && field.id > 0);
  const suggestions = $('mapping-parameters');
  suggestions.replaceChildren();
  for (const field of mappingParameters) suggestions.append(new Option(field.name, field.id));
  mappingFiltersState = metadata.available ? 'ready' : 'missing';
  drawMappingParameters();
}
async function loadMappingParameters(choice, {refresh = false} = {}) {
  const version = ++mappingEditorVersion;
  const cached = catalogueFilters.get(choice.key);
  if (cached?.available) {
    applyMappingParameters(cached);
    if (!refresh) return;
  } else {
    mappingFiltersState = 'loading';
    drawMappingParameters();
  }
  try {
    const params = new URLSearchParams({preset: choice.preset, group: choice.group});
    const metadata = await api('/api/filters?' + params);
    if (version !== mappingEditorVersion || !$('mapping-dialog').open) return;
    catalogueFilters.set(choice.key, metadata);
    applyMappingParameters(metadata);
  } catch (error) {
    if (version !== mappingEditorVersion || !$('mapping-dialog').open || cached?.available) return;
    mappingFiltersState = 'error';
    drawMappingParameters();
    $('mapping-filter-status').textContent = error.message;
  }
}
function catalogueQueryKey(query) {
  const sort = value => {
    if (Array.isArray(value)) return value.map(sort);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.keys(value).sort().map(key => [key, sort(value[key])]));
    }
    return value;
  };
  return JSON.stringify(sort(query));
}
function mappingRule(scope, field) {
  return scope && Object.hasOwn(scope.fields, field) ? scope.fields[field] : undefined;
}
function mappingCatalogues() {
  const byQuery = new Map(mappingScopes.map(scope => [catalogueQueryKey(scope.query), scope]));
  return presets.filter(preset => preset.config.source === collectionSource).flatMap(preset =>
    preset.config.groups.map(group => ({
      key: JSON.stringify([preset.path, group.key]),
      preset: preset.path,
      group: group.key,
      label: preset.name + ' → ' + group.name,
      scope: byQuery.get(catalogueQueryKey(group.query)),
    })));
}
function selectedMappingCatalogue() {
  if (!mappingCell || !$('mapping-dialog').open) return null;
  return mappingCell.choices.find(choice => choice.key === $('mapping-catalogue').value);
}
function enableMappingTabScroll(tabs) {
  tabs.addEventListener('wheel', event => {
    const mode = event.deltaMode;
    if (event.ctrlKey || event.shiftKey || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
    const scale = mode === 1 ? 16 : mode === 2 ? tabs.clientWidth : 1;
    const previous = tabs.scrollLeft;
    tabs.scrollLeft += event.deltaY * scale;
    if (tabs.scrollLeft !== previous) event.preventDefault();
  }, {passive: false});
}
function enableDragScroll(container, {vertical = false} = {}) {
  let drag, suppressClick = false;
  container.addEventListener('pointerdown', event => {
    if (event.pointerType !== 'mouse' || event.button !== 0) return;
    suppressClick = false;
    const canScrollX = container.scrollWidth > container.clientWidth;
    const canScrollY = vertical && container.scrollHeight > container.clientHeight;
    drag = canScrollX || canScrollY
      ? {id: event.pointerId, x: event.clientX, y: event.clientY,
        left: container.scrollLeft, top: container.scrollTop, canScrollX, canScrollY, active: false}
      : undefined;
  });
  container.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId || !(event.buttons & 1)) return;
    const dx = drag.canScrollX ? event.clientX - drag.x : 0;
    const dy = drag.canScrollY ? event.clientY - drag.y : 0;
    if (!drag.active) {
      if (Math.hypot(dx, dy) < 6) return;
      drag.active = suppressClick = true;
      container.setPointerCapture(event.pointerId);
      container.classList.add('dragging');
    }
    if (drag.canScrollX) container.scrollLeft = drag.left - dx;
    if (drag.canScrollY) container.scrollTop = drag.top - dy;
  });
  const finish = event => {
    if (drag?.id !== event.pointerId) return;
    drag = undefined;
    container.classList.remove('dragging');
  };
  for (const event of ['pointerup', 'pointercancel', 'lostpointercapture']) container.addEventListener(event, finish);
  container.addEventListener('click', event => {
    if (!suppressClick || !event.detail) return;
    suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
}
function drawMappingTabs() {
  const tabs = $('mapping-tabs');
  tabs.replaceChildren();
  for (const [index, sheet] of mappingTargets.entries()) {
    const button = document.createElement('button');
    button.id = 'mapping-tab-' + index;
    button.type = 'button';
    button.textContent = sheet.sheet;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', 'mapping-sheet-panel');
    if (sheet.groups.some(group => group.has_data === true)) {
      const complete = sheet.groups.every(group => group.complete === true);
      button.classList.add(complete ? 'mapping-tab-complete' : 'mapping-tab-partial');
      button.title = complete ? 'Во всех группах есть компоненты, обязательные поля заполнены'
        : 'Есть данные, лист заполнен частично';
      button.setAttribute('aria-label', sheet.sheet + '. ' + button.title);
    }
    button.addEventListener('click', () => selectMappingSheet(sheet.sheet));
    button.addEventListener('keydown', event => {
      const last = mappingTargets.length - 1;
      const positions = {ArrowLeft: index ? index - 1 : last, ArrowRight: index === last ? 0 : index + 1, Home: 0, End: last};
      if (!Object.hasOwn(positions, event.key)) return;
      event.preventDefault();
      selectMappingSheet(mappingTargets[positions[event.key]].sheet, true);
    });
    tabs.append(button);
  }
}
function selectMappingSheet(name, focus = false) {
  const changed = mappingSheet !== name;
  mappingSheet = name;
  const index = mappingTargets.findIndex(sheet => sheet.sheet === name);
  const tabs = $('mapping-tabs');
  for (const [position, button] of Array.from(tabs.children).entries()) {
    button.setAttribute('aria-selected', String(position === index));
    button.tabIndex = position === index ? 0 : -1;
  }
  const active = tabs.children[index];
  $('mapping-sheet-panel').setAttribute('aria-labelledby', active.id);
  if (focus) active.focus();
  if (changed || focus) {
    const right = active.offsetLeft + active.offsetWidth;
    if (right > tabs.scrollLeft + tabs.clientWidth) tabs.scrollLeft = right - tabs.clientWidth;
    else if (active.offsetLeft < tabs.scrollLeft) tabs.scrollLeft = active.offsetLeft;
    $('mapping-sheet-panel').querySelector('.table-wrap').scrollLeft = 0;
  }
  drawMappingSheet(mappingTargets[index]);
}
function mappingCellState(field, rules, configured) {
  if (field.state === 'fixed') return ['filled', String(field.value), 'Значение из матрицы'];
  if (field.state === 'excluded') return ['excluded', '—', 'Не заполняется'];
  if (field.state === 'unavailable') return ['excluded', '—', 'Поле больше недоступно в матрице'];
  if (field.state !== 'required') return ['excluded', '·', 'Требование не задано'];
  const active = rules.filter(rule => !rule.deferred);
  if (active.length) {
    const ids = [...new Set(active.map(rule => rule.parameter_id))];
    const label = ids.length === 1 ? 'ID ' + ids[0] : active.length + ' правил';
    return ['filled', label, 'Сопоставлено'];
  }
  if (rules.some(rule => rule.deferred)) return ['deferred', 'Позже', 'Дозаполнить позже'];
  return ['missing', '+', configured ? 'Нужно сопоставить' : 'Сначала заполните исходную группу аналитиков'];
}
function drawMappingSheet(sheet) {
  const head = $('mapping-head');
  const body = $('mapping-rules');
  head.replaceChildren();
  body.replaceChildren();
  const catalogues = mappingCatalogues();
  const groups = new Map(sheet.groups.map(group => [group.name, {...group, fields: [...group.fields]}]));
  const scopes = mappingScopes.filter(scope => scope.sheet === sheet.sheet);
  for (const scope of scopes) {
    if (!groups.has(scope.group)) groups.set(scope.group, {name: scope.group, configured: false, fields: []});
    const fields = groups.get(scope.group).fields;
    const names = new Set(fields.map(field => field.name));
    for (const name of Object.keys(scope.fields)) {
      if (!names.has(name)) fields.push({name, state: 'unavailable'});
    }
  }
  const columns = [...new Set([...groups.values()].flatMap(group => group.fields.map(field => field.name)))];
  const headings = head.insertRow();
  for (const label of ['Группа', ...columns]) {
    const th = document.createElement('th');
    th.scope = 'col';
    th.textContent = label;
    headings.append(th);
  }
  for (const group of groups.values()) {
    const row = body.insertRow();
    const title = document.createElement('th');
    title.scope = 'row';
    title.textContent = group.name;
    if (typeof group.has_data === 'boolean') {
      const status = document.createElement('span');
      status.className = 'mapping-group-status ' + (group.has_data ? 'mapping-filled' : 'mapping-missing');
      status.textContent = group.has_data ? 'Есть данные' : 'Нет данных';
      status.title = group.has_data ? 'В сохранённых сборах есть компоненты этой группы'
        : 'В сохранённых сборах нет компонентов этой группы';
      title.append(status);
    }
    row.append(title);
    const groupScopes = scopes.filter(scope => scope.group === group.name);
    const fields = new Map(group.fields.map(field => [field.name, field]));
    for (const column of columns) {
      const field = fields.get(column) || {name: column, state: 'unspecified'};
      const rules = groupScopes.map(scope => mappingRule(scope, column)).filter(Boolean);
      const [state, label, description] = mappingCellState(field, rules, group.configured);
      const cell = row.insertCell();
      cell.className = 'mapping-' + state;
      cell.title = description;
      if ((field.state === 'required' && group.configured) || rules.length) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.setAttribute('aria-label', [group.name, column, description, label].join('. '));
        button.addEventListener('click', () => openMappingCell(sheet.sheet, group, field, catalogues));
        cell.append(button);
      } else {
        cell.textContent = label;
        cell.setAttribute('aria-label', description + '. ' + label);
      }
    }
  }
}
function openMappingCell(sheet, group, field, catalogues) {
  const choices = catalogues.filter(choice =>
    choice.scope ? choice.scope.sheet === sheet && choice.scope.group === group.name
      : group.configured && field.state === 'required');
  mappingCell = {sheet, group: group.name, configured: group.configured, field, choices};
  const select = $('mapping-catalogue');
  select.replaceChildren(new Option('Выберите подкатегорию…', ''));
  for (const choice of choices) select.append(new Option(choice.label, choice.key));
  const preferred = choices.find(choice => choice.preset === pendingMappingParameter?.preset && choice.group === pendingMappingParameter.group)
    || choices.find(choice => mappingRule(choice.scope, field.name))
    || choices.find(choice => choice.scope && choice.preset === $('filter-preset').value && choice.group === $('filter-group').value)
    || choices.find(choice => choice.scope)
    || choices.find(choice => choice.preset === $('filter-preset').value && choice.group === $('filter-group').value);
  select.value = (preferred || (choices.length === 1 ? choices[0] : null))?.key || '';
  $('mapping-title').textContent = field.name;
  $('mapping-context').textContent = sheet + ' → ' + group.name;
  $('mapping-dialog').showModal();
  refreshMappingEditor();
}
async function refreshMappingEditor() {
  mappingEditorVersion += 1;
  const choice = selectedMappingCatalogue();
  const rule = choice && mappingRule(choice.scope, mappingCell.field.name);
  const suggestion = choice && choice.preset === pendingMappingParameter?.preset && choice.group === pendingMappingParameter.group
    ? pendingMappingParameter.field : null;
  mappingParameters = [];
  mappingFiltersState = 'idle';
  $('mapping-filter-search').value = '';
  $('mapping-parameters').replaceChildren();
  $('mapping-parameter').value = rule?.parameter_id || suggestion?.id || '';
  $('mapping-name').value = rule?.parameter_name || suggestion?.name || '';
  $('mapping-factor').value = rule?.factor || '1';
  $('remove-mapping').hidden = !rule;
  let status = 'Выберите подкатегорию каталога.';
  if (!mappingCell.choices.length) status = 'Добавьте подкатегорию каталога или освободите её от правил другой группы аналитиков.';
  else if (choice) status = rule?.deferred ? 'Дозаполнить позже' : rule ? 'Сопоставлено' : 'Без сопоставления';
  $('mapping-editor-status').textContent = status;
  drawMappingParameters();
  busyControls();
  if (!choice || !mappingCell.configured || mappingCell.field.state !== 'required') return;
  await loadMappingParameters(choice);
}
async function refreshMappings() {
  const version = ++mappingVersion;
  if (placeholderSelected() || collectionSource !== 'yageo') return;
  const message = $('mapping-empty');
  if (!mappingTargets.length) {
    message.hidden = false;
    message.textContent = 'Загрузка листов…';
  }
  const restartMessage = 'Сервер запущен со старой версией матрицы. Перезапустите uv run ui и обновите страницу.';
  try {
    const data = await api('/api/catalogue-mappings?' + new URLSearchParams({source: collectionSource}));
    if (version !== mappingVersion) return;
    if (!Array.isArray(data.targets) || !Array.isArray(data.scopes)) throw new Error(restartMessage);
    mappingScopes = data.scopes;
    mappingTargets = data.targets;
    message.hidden = !!mappingTargets.length;
    message.textContent = 'В матрице пока нет листов.';
    $('mapping-sheet-panel').hidden = !mappingTargets.length;
    drawMappingTabs();
    if (mappingTargets.length) {
      const preferred = mappingScopes.find(scope =>
        catalogueQueryKey(scope.query) === catalogueQueryKey(
          presets.find(preset => preset.path === $('filter-preset').value)?.config.groups
            .find(group => group.key === $('filter-group').value)?.query));
      const target = [mappingSheet, preferred?.sheet, mappingTargets[0].sheet]
        .find(name => mappingTargets.some(sheet => sheet.sheet === name));
      selectMappingSheet(target);
    }
  } catch (error) {
    if (version !== mappingVersion) return;
    message.hidden = false;
    message.textContent = error.message === 'Выберите конфигурацию источника' || error.message === restartMessage
      ? restartMessage : 'Не удалось загрузить листы. ' + error.message;
  }
}
async function saveMapping(mapping) {
  const choice = selectedMappingCatalogue();
  if (submitting || !choice) return;
  const cell = mappingCell;
  const {sheet, group, field} = cell;
  submitting = true;
  busyControls();
  try {
    const body = {preset: choice.preset, catalogue_group: choice.group, sheet, group, field: field.name, mapping};
    await api('/api/catalogue-mappings', {method: 'POST', body: JSON.stringify(body)});
    pendingMappingParameter = undefined;
    if (mappingCell === cell && $('mapping-dialog').open) $('mapping-dialog').close();
    await Promise.all([refreshMappings(), refreshStatus()]);
    notice(mapping?.deferred ? 'Поле отмечено для дозаполнения.' : mapping ? 'Правило сохранено.' : 'Отметка или правило удалены.', true);
  } catch (error) {
    $('mapping-editor-status').textContent = error.message;
    notice(error.message);
  } finally {
    submitting = false;
    busyControls();
  }
}
async function refreshRequirements() {
  try {
    const matrix = await api('/api/requirements');
    const groups = matrix.sheets.reduce((sum, sheet) => sum + sheet.configured, 0);
    $('requirements-summary').textContent = matrix.sheets.length ?
      `Листов: ${number(matrix.sheets.length)} · групп с требованиями: ${number(groups)}` : 'Таблицы аналитиков пока отсутствуют.';
    $('requirements-download').hidden = !matrix.sheets.length;
    $('requirements-details').hidden = !matrix.sheets.length;
    const list = $('requirements-sheets');
    list.replaceChildren();
    for (const sheet of matrix.sheets) {
      const item = document.createElement('li');
      item.textContent = `${sheet.name} · ${number(sheet.configured)} из ${number(sheet.total)}`;
      list.append(item);
    }
  } catch (error) { notice(error.message); }
}
async function launchJob(endpoint, data, output) {
  submitting = true;
  busyControls();
  try {
    const launched = await api(endpoint, {method: 'POST', body: JSON.stringify(data)});
    if (output !== undefined) selectRun(output);
    updateJob(launched, endpoint !== '/api/stop');
    await refreshStatus();
    if (!job.running && activePage === 'sources') await refreshSourceTools();
  } catch (error) { notice(error.message); }
  finally { submitting = false; busyControls(); }
}
function cancelDeleteHold() {
  clearTimeout(deleteTimer);
  deleteTimer = undefined;
  $('delete-result').classList.remove('holding');
}
function startDeleteHold() {
  const button = $('delete-result');
  if (button.disabled || deleteTimer !== undefined) return;
  const output = selectedOutput;
  button.classList.add('holding');
  deleteTimer = setTimeout(async () => {
    cancelDeleteHold();
    if (button.disabled || selectedOutput !== output) return;
    deletingResult = true;
    notice('');
    busyControls();
    try {
      job = await api('/api/delete-result', {method: 'POST', body: JSON.stringify({output})});
      outputs = outputs.filter(savedOutput => savedOutput !== output);
      newFolder();
      selectRun('');
      document.querySelector('[name=mode][value=new]').checked = true;
      offset = 0;
      drawStatus({job, summary: {unique_components: 0, groups: []}, files: []});
      notice('Сбор удалён.', true);
    } catch (error) {
      notice(error.message);
    } finally {
      deletingResult = false;
      busyControls();
    }
  }, DELETE_HOLD_MS);
}
const deleteButton = $('delete-result');
deleteButton.style.setProperty('--delete-hold-duration', `${DELETE_HOLD_MS}ms`);
deleteButton.addEventListener('pointerdown', event => {
  if (event.isPrimary && event.button === 0) startDeleteHold();
});
for (const event of ['pointerup', 'pointercancel']) window.addEventListener(event, cancelDeleteHold);
deleteButton.addEventListener('pointerleave', cancelDeleteHold);
deleteButton.addEventListener('pointermove', event => {
  if (deleteTimer === undefined) return;
  const rect = deleteButton.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
    cancelDeleteHold();
  }
});
deleteButton.addEventListener('keydown', event => {
  if (event.key !== ' ' && event.key !== 'Enter') return;
  event.preventDefault();
  if (!event.repeat) startDeleteHold();
});
deleteButton.addEventListener('keyup', event => {
  if (event.key === ' ' || event.key === 'Enter') cancelDeleteHold();
});
deleteButton.addEventListener('blur', cancelDeleteHold);
deleteButton.addEventListener('contextmenu', event => event.preventDefault());
window.addEventListener('blur', cancelDeleteHold);
document.addEventListener('visibilitychange', cancelDeleteHold);

$('preset').addEventListener('change', () => {
  renderSubcategories(selectedCategory());
  chooseSubcategory();
});
$('subcategory').addEventListener('change', chooseSubcategory);
$('source').addEventListener('change', () => {
  sessionStorage.setItem('selected-source', $('source').value);
  if (!placeholderSelected() && collectionSource !== $('source').value) {
    collectionSource = $('source').value;
    renderPresets(collectionSource);
    renderSubcategories(selectedCategory());
    chooseSubcategory();
    drawSourceTools();
  }
  cancelDeleteHold();
  busyControls();
  openPage();
});
for(const id of ['max-pages','max-parts','page-size']) $(id).addEventListener('input',estimate);
$('groups').addEventListener('input',tags);
$('collect-form').addEventListener('submit',async(event)=>{
  event.preventDefault(); notice('');
  try {
    const config={source:collectionSource,name:selectedCategory()?.name ?? null,options:JSON.parse($('adapter-options').value),page_size:Number($('page-size').value),download_delay:Number($('delay').value),groups:JSON.parse($('groups').value)};
    const output=$('output').value.trim();
    await launchJob('/api/start', {output, mode: document.querySelector('[name=mode]:checked').value, config,
      max_pages: Number($('max-pages').value), max_parts: Number($('max-parts').value),
      log_level: $('log-level').value, no_export: $('no-export').checked}, output);
  } catch(error){notice(error.message);}
});
$('stop').addEventListener('click', () => launchJob('/api/stop', {}));
$('export').addEventListener('click', () => launchJob('/api/export', {output: selectedOutput}));
$('datasheets').addEventListener('click', () => {
  if ($('max-documents').reportValidity()) launchJob('/api/datasheets', {output: selectedOutput, max_documents: Number($('max-documents').value)});
});
$('coverage-report').addEventListener('click', () => launchJob('/api/coverage-report', {output: selectedOutput}));
$('convert-requirements').addEventListener('click', () => launchJob('/api/requirements', {}));
$('filter-preset').addEventListener('change', () => {
  drawFilterGroups(); pendingMappingParameter = undefined; busyControls(); refreshFilters();
});
$('filter-group').addEventListener('change', () => {
  pendingMappingParameter = undefined; refreshFilters();
});
$('close-mapping').addEventListener('click', () => $('mapping-dialog').close());
enableMappingTabScroll($('mapping-tabs'));
enableDragScroll($('mapping-tabs'));
enableDragScroll($('mapping-sheet-panel').querySelector('.mapping-table'), {vertical: true});
enableDragScroll($('reader-groups'));
enableDragScroll($('reader-table-wrap'), {vertical: true});
enableDragScroll($('part-table-wrap'), {vertical: true});
$('mapping-dialog').addEventListener('close', () => {
  mappingEditorVersion += 1;
  mappingCell = undefined;
  busyControls();
});
$('mapping-catalogue').addEventListener('change', refreshMappingEditor);
$('mapping-filter-search').addEventListener('input', drawMappingParameters);
$('fetch-mapping-filters').addEventListener('click', () => {
  const choice = selectedMappingCatalogue();
  if (!choice) return;
  launchJob('/api/filters', {preset: choice.preset, group: choice.group});
});
$('defer-mapping').addEventListener('click', () => saveMapping({deferred: true}));
$('remove-mapping').addEventListener('click', () => saveMapping(null));
$('mapping-parameter').addEventListener('input', () => showMappingParameter(true));
$('mapping-form').addEventListener('submit', event => {
  event.preventDefault();
  saveMapping({
    parameter_id: Number($('mapping-parameter').value),
    parameter_name: $('mapping-name').value,
    factor: $('mapping-factor').value,
  });
});
$('filters-form').addEventListener('submit', event => {
  event.preventDefault();
  launchJob('/api/filters', {preset: $('filter-preset').value, group: $('filter-group').value});
});
$('preset-category').addEventListener('change', updatePresetCategory);
$('preset-request-url').addEventListener('input', () => { $('preset-query-preview').hidden = true; });
$('preview-preset').addEventListener('click', async () => {
  const input = $('preset-request-url');
  if (!input.reportValidity()) return;
  const request_url = input.value.trim();
  const preview = $('preset-query-preview');
  preview.hidden = true;
  try {
    const result = await api('/api/presets/yageo/preview', {method: 'POST', body: JSON.stringify({request_url})});
    if (request_url !== input.value.trim()) return;
    preview.textContent = JSON.stringify(result.query, null, 2);
    preview.hidden = false;
  } catch (error) {
    if (request_url === input.value.trim()) notice(error.message);
  }
});
$('preset-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  submitting = true; busyControls();
  try {
    const data = Object.fromEntries(new FormData(form));
    for (const key of Object.keys(data)) data[key] = data[key].trim();
    const isNew = data.category_file === 'new';
    if (isNew) delete data.category_file;
    const saved = await api('/api/presets/yageo', {method: 'POST', body: JSON.stringify(data)});
    form.reset();
    $('preset-query-preview').hidden = true;
    await listConfigs();
    $('preset-category').value = saved.path;
    updatePresetCategory();
    $('filter-preset').value = saved.path;
    drawFilterGroups();
    $('filter-group').value = saved.group;
    mappingSheet = undefined;
    pendingMappingParameter = undefined;
    await refreshCatalogueTools();
    notice(isNew ? 'Категория и подкатегория созданы.' : 'Подкатегория добавлена.', true);
  } catch (error) { notice(error.message); }
  finally { submitting = false; busyControls(); }
});
$('supplement-file').addEventListener('change', busyControls);
$('supplement-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  data.set('output', selectedOutput);
  submitting = true; busyControls();
  try {
    const result = await api('/api/import-supplements', {method: 'POST', body: data});
    form.reset();
    await refreshStatus();
    notice(`Добавлено записей: ${number(result.imported)}.`, true);
  } catch (error) { notice(error.message); }
  finally { submitting = false; busyControls(); }
});
$('existing').addEventListener('change', async () => {
  const output = $('existing').value;
  selectRun(output, true);
  if (!output) {
    newFolder();
    document.querySelector('[name=mode][value=new]').checked = true;
  }
  const data = await refreshStatus();
  if (data && output) notice(hasSavedResult ? 'Сохранённый сбор открыт.' : 'Выбранный сбор недоступен.', hasSavedResult);
});
document.querySelector('[name=mode][value=new]').addEventListener('change', newFolder);
$('refresh-workspace').addEventListener('click', async () => {
  try {
    await listConfigs();
    if (activePage === 'sources') await refreshSourceTools();
    if (activePage === 'reader') await refreshReader();
    await refreshStatus();
  } catch (error) { notice(error.message); }
});
$('reader-category').addEventListener('change', () => {
  readerCategory = $('reader-category').value;
  readerPartVersion += 1;
  chooseReaderCategory();
  refreshReaderParts();
});
$('reader-stats-level').addEventListener('change', drawReaderStatistics);
$('reader-search').addEventListener('input', () => {
  clearTimeout(readerSearchTimer);
  readerPartVersion += 1;
  readerRowsVersion += 1;
  readerSearchTimer = setTimeout(() => {
    readerOffset = 0;
    if (activePage === 'reader') refreshReaderParts();
  }, 220);
});
$('reader-first').addEventListener('click', () => goToReaderPage(0));
$('reader-prev').addEventListener('click', () => goToReaderPage(readerOffset - readerLimit));
$('reader-next').addEventListener('click', () => goToReaderPage(readerOffset + readerLimit));
$('reader-last').addEventListener('click', () => goToReaderPage(Math.floor((readerTotal - 1) / readerLimit) * readerLimit));
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{offset=0;refreshParts();},250);});
$('prev').addEventListener('click',()=>{if(!tableBusy){offset=Math.max(0,offset-25);refreshParts();}});
$('next').addEventListener('click',()=>{if(!tableBusy){offset+=25;refreshParts();}});
$('close-dialog').onclick=()=>$('part-dialog').close();

const helpDialog = $('field-help');
if (helpDialog) {
  document.addEventListener('click', event => {
    const trigger = event.target.closest?.('button[data-help]');
    const topic = trigger && $(`help-${trigger.dataset.help}`);
    if (!topic) return;
    $('field-help-title').textContent = topic.dataset.title;
    $('field-help-content').replaceChildren(topic.content.cloneNode(true));
    helpDialog.scrollTop = 0;
    if (!helpDialog.open) helpDialog.showModal();
  });
  helpDialog.addEventListener('click', event => {
    if (event.target === helpDialog) helpDialog.close();
  });
}

(async()=>{
  try{
    showPage();
    mountCoverage();
    await listConfigs();
    if(!sources.length)throw new Error('Не зарегистрированы адаптеры источников.');
    chooseSubcategory();
    const data = await api('/api/status?output=');
    const previous = sessionStorage.getItem('selected-output');
    const output = (data.job.running && data.job.output) || (outputs.includes(previous) && previous) ||
      (activePage === 'collect' ? '' : outputs[0]) || '';
    selectRun(output, true);
    if (activePage === 'sources') refreshSourceTools();
    if (activePage === 'reader') refreshReader();
    await refreshStatus();
    window.addEventListener('focus', () => refreshStatus());
  }catch(error){notice(error.message);}
})();
