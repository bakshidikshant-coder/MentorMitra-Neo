const S = sessionStorage, $ = s => document.querySelector(s);
let tok = S.tok || '', me = S.me ? JSON.parse(S.me) : null, tab = '', mf = { q: '', risk: '' };
const E = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const LBL = { exam_form: 'Examination form', informed: 'Student informed', sessional: 'Sessional requirements', fees: 'Fees' };
const ST = { exam_form: ['not_submitted', 'submitted'], informed: ['not_informed', 'informed'], sessional: ['pending', 'completed'], fees: ['pending', 'partly_paid', 'paid'] };
const nice = s => E(String(s || '').replace(/_/g, ' '));
const bdg = u => `<span class="b ${E(u)}">${nice(u)}</span>`;
const card = (n, l) => `<div class=card><b>${n}</b><span>${l}</span></div>`;
const obj = fd => Object.fromEntries([...fd].filter(([, v]) => v !== ''));
const tbl = (h, rows) => rows.length ? `<div class=tw><table><tr>${h.map(x => `<th>${x}</th>`).join('')}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</table></div>` : '<p class=muted>Nothing to show.</p>';

function toast(m, bad) { const t = $('#toast'); t.textContent = m; t.className = 'show' + (bad ? ' bad' : ''); clearTimeout(toast.t); toast.t = setTimeout(() => t.className = '', 3500); }
async function api(p, o = {}) {
  const h = {}; if (tok) h.Authorization = 'Bearer ' + tok;
  let b = o.body; if (b && !(b instanceof FormData)) { h['Content-Type'] = 'application/json'; b = JSON.stringify(b); }
  const r = await fetch('/api' + p, { method: o.method || 'GET', headers: h, body: b });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && tok) { logout(); throw Error('Session expired, please sign in again'); }
  if (!r.ok) { const e = Error(j.error || 'Request failed'); e.data = j; throw e; }
  return j;
}
const modal = h => { $('#mb').innerHTML = h; $('#modal').hidden = false; };
const post = (p, body, method = 'POST') => api(p, { method, body });

/* ---------- student ---------- */
const reqTable = rs => tbl(['Requirement', 'Status', 'Deadline', 'Urgency', 'Remarks', ''], rs.map(r => [
  LBL[r.type], nice(r.status) + (r.type === 'fees' && r.amount_due ? ` (₹${r.amount_due} due)` : ''), r.deadline || '—', bdg(r.urgency), E(r.remarks || ''),
  r.is_done ? '✓' : r.claimed_at ? '<i>awaiting mentor</i>' : `<button data-a=claim data-id=${r.id}>Mark as done</button>`]));
async function stuHome() {
  const d = await api('/student/dashboard'), p = d.profile;
  return `<h2>Hello, ${E(p.name)}</h2><p class=muted>${E(p.programme)} · Sem ${p.semester} · Sec ${E(p.section)} · Mentor: <b>${d.mentor ? E(d.mentor.name) + ' (' + E(d.mentor.email) + ')' : 'not assigned yet'}</b></p>
  <div class=cards>${card(d.pending, 'Pending')}${card(d.overdue, 'Overdue')}${card(d.open_queries, 'Open queries')}${card(d.unread_notifications, 'Unread')}</div><h2>My requirements</h2>${reqTable(d.requirements)}`;
}
async function stuQ() {
  const q = await api('/student/queries');
  return `<h2>Raise a query</h2><form data-f=newq class=row><select name=category>${['exam_form', 'sessional', 'fees', 'academic', 'other'].map(c => `<option>${c}</option>`).join('')}</select>
  <input name=subject placeholder=Subject required><textarea name=description placeholder="Describe the issue" required></textarea><button class=pri>Submit</button></form>
  <h2>My queries</h2>` + tbl(['#', 'Subject', 'Category', 'Status', 'Updated', ''], q.map(x => [x.id, E(x.subject), nice(x.category), bdg(x.status), x.updated_at.slice(0, 10), `<button data-a=thr data-r=student data-id=${x.id}>Open</button>`]));
}
async function notes() {
  const n = await api('/student/notifications'); post('/student/notifications/read', {}).catch(() => 0);
  return '<h2>Notifications</h2>' + tbl(['When', 'Title', 'Message'], n.map(x => [x.created_at.slice(0, 16).replace('T', ' '), (x.read_at ? '' : '● ') + E(x.title), E(x.body)]));
}
async function thread(r, id) {
  const q = await api(`/${r}/queries/${id}`);
  modal(`<h3>#${q.id} ${E(q.subject)} ${bdg(q.status)}</h3><p>${E(q.description)}</p><div class=msgs>${q.messages.map(m => `<p><b>${E(m.author)}</b> <small>${m.created_at.slice(0, 16).replace('T', ' ')}</small><br>${E(m.body)}</p>`).join('') || '<p class=muted>No replies yet.</p>'}</div>
  <form data-f=reply data-r=${r} data-id=${id} class=row><textarea name=body required placeholder="Write a reply"></textarea><button class=pri>Send</button></form>` +
    (r === 'mentor' ? `<form data-f=qstat data-id=${id} class=row><select name=status>${['open', 'in_progress', 'escalated', 'resolved'].map(s => `<option ${s === q.status ? 'selected' : ''}>${s}</option>`).join('')}</select><input name=escalated_to placeholder="Escalate to (department/admin)"><button>Update status</button></form>` : ''));
}

/* ---------- mentor ---------- */
async function mDash() {
  const d = await api('/mentor/dashboard');
  return `<h2>Overview · ${E(d.session)}</h2><div class=cards>${card(d.mentees, 'Mentees')}${card(d.overdue_items, 'Overdue items')}${card(d.open_queries, 'Open queries')}${card(d.awaiting_verification, 'To verify')}${card(d.high_risk, 'High risk')}${card(d.followups_missed, 'Missed follow-ups')}</div>
  <h2>Needs attention</h2>` + tbl(['Student', 'Class', 'Worst', 'Risk', 'Why', ''], d.needs_attention.map(m => [`${E(m.name)} <small>${E(m.roll_no)}</small>`, E(m.class), bdg(m.worst_urgency), bdg(m.risk.level), E(m.risk.reasons.join(', ')), `<button data-a=manage data-id=${m.student_id}>Manage</button>`]));
}
async function mMentees() {
  const l = await api('/mentor/mentees?' + new URLSearchParams(mf));
  return `<h2>My mentees</h2><div class=row><input data-flt=q placeholder="Search name / roll" value="${E(mf.q)}"><select data-flt=risk>${['', 'high', 'medium', 'low'].map(r => `<option value="${r}" ${r === mf.risk ? 'selected' : ''}>${r ? r + ' risk' : 'any risk'}</option>`).join('')}</select><button data-a=remind>Send reminder to selected</button></div>` +
    tbl(['', 'Student', 'Class', 'Requirements', 'Risk', 'Queries', ''], l.map(m => [`<input type=checkbox class=sel value=${m.student_id}>`, `${E(m.name)}<br><small>${E(m.roll_no)}</small>`, E(m.class),
      m.requirements.map(r => `<span class="b ${r.urgency}" title="${LBL[r.type]}: ${nice(r.status)}">${r.type.slice(0, 4)}</span>`).join(''), `<span title="${E(m.risk.reasons.join(', '))}">${bdg(m.risk.level)}</span>`, m.open_queries, `<button data-a=manage data-id=${m.student_id}>Manage</button>`]));
}
async function mQ() {
  const q = await api('/mentor/queries');
  return '<h2>Student queries</h2>' + tbl(['#', 'Student', 'Subject', 'Category', 'Status', ''], q.map(x => [x.id, `${E(x.student_name)} <small>${E(x.roll_no)}</small>`, E(x.subject), nice(x.category), bdg(x.status), `<button data-a=thr data-r=mentor data-id=${x.id}>Open</button>`]));
}
async function mCal() {
  const c = await api('/mentor/calendar'), t = rows => tbl(['Date', 'Student', 'Action', 'Note', ''], rows.map(f => [f.next_followup_date, `${E(f.student_name)} <small>${E(f.roll_no)}</small>`, f.action, E(f.note || ''), `<button data-a=fdone data-id=${f.id}>Mark done</button>`]));
  return `<h2 class=err>Missed (${c.missed.length})</h2>${t(c.missed)}<h2>Upcoming (${c.upcoming.length})</h2>${t(c.upcoming)}`;
}
async function manage(id) {
  const m = await api('/mentor/mentees/' + id);
  modal(`<h3>${E(m.name)} <small>${E(m.roll_no)} · ${E(m.class)}</small></h3><p>${bdg(m.risk.level)} ${E(m.risk.reasons.join(', ') || 'No concerns')}</p>` +
    m.requirements.map(r => `<form data-f=req data-id=${r.id} data-s=${id} class="row req"><b>${LBL[r.type]}</b> ${bdg(r.urgency)}${r.claimed_at && r.urgency !== 'done' ? ' <i>student says done</i>' : ''}
      <select name=status>${ST[r.type].map(s => `<option ${s === r.status ? 'selected' : ''}>${s}</option>`).join('')}</select><input type=date name=deadline value="${r.deadline || ''}">
      ${r.type === 'fees' ? `<input name=amount_due type=number min=0 placeholder="Amount due" value="${r.amount_due ?? ''}">` : ''}<input name=remarks placeholder=Remarks value="${E(r.remarks || '')}"><button>Save</button></form>`).join('') +
    `<h4>Log follow-up</h4><form data-f=fu data-s=${id} class=row><select name=action>${['call', 'meeting', 'email', 'note', 'reminder'].map(a => `<option>${a}</option>`).join('')}</select><input name=note placeholder=Note><input type=date name=next_followup_date title="Next follow-up"><button>Add</button></form>` +
    tbl(['Date', 'Action', 'Note', 'Next'], m.followups.slice(0, 8).map(f => [f.created_at.slice(0, 10), f.action, E(f.note || ''), f.next_followup_date || ''])));
}

/* ---------- admin ---------- */
async function aAlloc() {
  const s = await api('/admin/mentors/summary'), adm = me.role === 'admin';
  return (adm ? `<h2>1. Upload subject allocation</h2><form data-f=upload class=row><input type=file name=file accept=".csv,.xlsx" required><button>Validate</button></form><p class=muted>Columns: teacher, subject, programme, semester, section</p><div id=prev></div>
  <h2>2. Allocate mentors</h2><label><span><input type=checkbox id=keep checked> keep existing mentors</span></label><div class=row><button data-a=alloc data-dry=1>Dry run</button><button class=pri data-a=alloc data-dry=0>Apply</button></div><div id=res></div>` : '') +
    '<h2>Mentor load</h2>' + tbl(['Teacher', 'Mentees', 'Capacity', ''], s.teachers.map(t => [E(t.name), t.mentees, t.capacity, `<progress max=${t.capacity || 1} value=${t.mentees}></progress>`])) +
    `<h2>Students without a mentor (${s.unassigned_students.length})</h2>` + tbl(['Roll no', 'Class'], s.unassigned_students.map(u => [E(u.roll_no), E(`${u.programme}-${u.semester}-${u.section}`)]));
}
async function aSetup() {
  const [t, s] = await Promise.all([api('/admin/teachers'), api('/admin/settings')]), inp = (n, l, x = '') => `<label>${l}<input name=${n} ${x}></label>`;
  return `<h2>Settings</h2><form data-f=settings class=row>${inp('current_session', 'Session', `value="${E(s.current_session)}"`)}${inp('default_capacity', 'Default capacity', `type=number min=0 value=${s.default_capacity}`)}${inp('due_soon_days', 'Due-soon days', `type=number min=0 value=${s.due_soon_days}`)}<button>Save</button></form>
  <h2>Requirement deadlines</h2><form data-f=init class=row>${Object.keys(LBL).map(k => `<label>${LBL[k]}<input type=date name=${k}></label>`).join('')}<button>Create for all students</button></form>
  <h2>Import students (CSV)</h2><form data-f=students class=row><input type=file name=file accept=.csv required><button>Import</button></form><p class=muted>Columns: roll_no, name, email, programme, semester, section</p><div id=out></div>
  <h2>Add teacher</h2><form data-f=teacher class=row>${inp('name', 'Name', 'required')}${inp('email', 'Email', 'type=email required')}${inp('employee_code', 'Employee code', 'required')}${inp('department', 'Department')}${inp('capacity_override', 'Capacity override', 'type=number min=0')}<button>Add</button></form><div id=out2></div>
  <h2>Teachers</h2>` + tbl(['Code', 'Name', 'Dept', 'Mentees', 'Capacity', 'Active', ''], t.map(x => [E(x.employee_code), E(x.name), E(x.department || ''), x.mentees, x.capacity_override ?? 'default', x.active ? 'yes' : 'no',
    `<button data-a=cap data-id=${x.id}>Capacity</button> <button data-a=tog data-id=${x.id} data-on=${x.active ? 0 : 1}>${x.active ? 'Deactivate' : 'Activate'}</button>`]));
}

/* ---------- wiring ---------- */
const TABS = {
  student: { Overview: stuHome, Queries: stuQ, Notifications: notes },
  mentor: { Dashboard: mDash, Mentees: mMentees, Queries: mQ, Calendar: mCal, Notifications: notes },
  admin: { Allocation: aAlloc, Setup: aSetup }, hod: { Allocation: aAlloc }
};
async function show(t) {
  tab = t; document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  try { $('#main').innerHTML = await TABS[me.role][t](); } catch (e) { $('#main').innerHTML = `<p class=err>${E(e.message)}</p>`; }
}
function boot() {
  $('#login').hidden = !!me; $('#app').hidden = !me; if (!me) return;
  $('#who').textContent = `${me.name} (${me.role})`;
  const names = Object.keys(TABS[me.role]); $('#nav').innerHTML = names.map(t => `<button data-a=tab data-t="${t}">${t}</button>`).join(''); show(names[0]);
}
function logout() { S.clear(); tok = ''; me = null; $('#modal').hidden = true; boot(); }
const reload = () => show(tab);
const A = {
  tab: a => show(a.dataset.t), logout, close: () => $('#modal').hidden = true,
  async claim(a) { const n = prompt('Add a note for your mentor (optional)'); if (n === null) return; await post(`/student/requirements/${a.dataset.id}/claim`, { note: n }); toast('Reported to your mentor'); reload(); },
  thr: a => thread(a.dataset.r, a.dataset.id), manage: a => manage(a.dataset.id),
  async remind() {
    const ids = [...document.querySelectorAll('.sel:checked')].map(x => +x.value); if (!ids.length) return toast('Select at least one student', 1);
    const message = prompt('Message (leave blank for an automatic reminder)'); if (message === null) return;
    const channels = (prompt('Channels: in_app, email, sms (comma separated)', 'in_app') || 'in_app').split(',').map(s => s.trim());
    const r = await post('/mentor/reminders', { student_ids: ids, message: message || undefined, channels }); toast(`Reminder sent to ${r.sent.length}, skipped ${r.skipped_nothing_pending.length} (nothing pending)`);
  },
  async fdone(a) { await post('/mentor/followups/' + a.dataset.id, { done: true }, 'PATCH'); reload(); },
  async confirm(a) { const r = await post('/admin/allocations/confirm', { token: a.dataset.t }); toast(`Allocation saved (${r.rows} rows). Now run mentor allocation.`); reload(); },
  async alloc(a) {
    const dry = a.dataset.dry === '1'; if (!dry && !confirm('Apply this allocation to the database?')) return;
    const d = await post('/admin/mentors/allocate', { dry_run: dry, keep_existing: $('#keep').checked }), s = d.summary;
    if (!dry) toast('Mentors allocated'); 
    $('#res').innerHTML = `<p><b>${dry ? 'Preview' : 'Applied'}:</b> assign ${s.to_assign}, release ${s.to_release}, unassigned ${s.unassigned}</p>` +
      tbl(['Teacher', 'Capacity', 'Before', 'After'], d.distribution.map(x => [E(x.name), x.capacity, x.load_before, x.load_after])) +
      (d.unassigned.length ? `<p class=err>${d.unassigned.length} student(s) cannot be assigned (${[...new Set(d.unassigned.map(u => u.reason))].join(', ').replace(/_/g, ' ')}).</p>` : '');
  },
  async cap(a) { const v = prompt('Capacity override (blank = use default)'); if (v === null) return; await post('/admin/teachers/' + a.dataset.id, { capacity_override: v === '' ? null : +v }, 'PATCH'); reload(); },
  async tog(a) { const r = await post('/admin/teachers/' + a.dataset.id, { active: a.dataset.on === '1' }, 'PATCH'); toast(r.mentees_released ? `${r.mentees_released} mentees released: run allocation again` : 'Updated'); reload(); }
};
const F = {
  async login(fd) { const r = await post('/auth/login', obj(fd)); S.tok = tok = r.token; S.me = JSON.stringify(r.user); me = r.user; boot(); },
  async newq(fd, f) {
    const r = await post('/student/queries', obj(fd)); f.reset();
    modal(`<h3>Query submitted</h3>${r.mentor_assigned ? '' : '<p class=err>You have no mentor yet; the query is saved.</p>'}${r.suggested_answers.length ? '<p>These answers may help meanwhile:</p>' + r.suggested_answers.map(x => `<p><b>${E(x.question)}</b><br>${E(x.answer)}</p>`).join('') : ''}`); reload();
  },
  async reply(fd, f) { await post(`/${f.dataset.r}/queries/${f.dataset.id}/messages`, obj(fd)); thread(f.dataset.r, f.dataset.id); },
  async qstat(fd, f) { await post('/mentor/queries/' + f.dataset.id, obj(fd), 'PATCH'); toast('Status updated'); thread('mentor', f.dataset.id); },
  async req(fd, f) { const b = obj(fd); if (b.amount_due) b.amount_due = +b.amount_due; if (!fd.get('deadline')) b.deadline = null; await post('/mentor/requirements/' + f.dataset.id, b, 'PATCH'); toast('Saved'); manage(f.dataset.s); },
  async fu(fd, f) { await post('/mentor/followups', { ...obj(fd), student_id: +f.dataset.s }); toast('Follow-up logged'); manage(f.dataset.s); },
  async upload(fd) {
    let d; try { d = await api('/admin/allocations/preview', { method: 'POST', body: fd }); } catch (e) { if (!e.data || !e.data.errors) throw e; d = e.data; }
    $('#prev').innerHTML = `<p><b>${d.valid_rows}</b> valid rows, <b class=err>${d.error_count}</b> errors, ${d.warnings.length} warnings.</p>` + (d.errors.length ? tbl(['Row', 'Field', 'Problem'], d.errors.map(x => [x.row, x.field, E(x.message)])) : '') +
      (d.warnings.length ? tbl(['Row', 'Warning'], d.warnings.map(w => [w.row, E(w.message)])) : '') + (d.can_confirm ? `<button class=pri data-a=confirm data-t=${d.token}>Confirm upload</button>` : '<p class=err>Fix the errors and upload again.</p>');
  },
  async settings(fd) { await post('/admin/settings', obj(fd), 'PUT'); toast('Settings saved'); },
  async init(fd) { const r = await post('/admin/requirements/initialize', { deadlines: obj(fd) }); toast(`${r.created} requirement rows created`); },
  async students(fd) {
    const r = await api('/admin/students/import', { method: 'POST', body: fd });
    $('#out').innerHTML = `<p><b>${r.created}</b> students created, ${r.errors.length} rows rejected.</p>` + (r.errors.length ? tbl(['Row', 'Problem'], r.errors.map(e => [e.row, E(e.message)])) : '') +
      (r.credentials.length ? '<p class=err>Copy these temporary passwords now; they are not shown again.</p>' + tbl(['Roll no', 'Email', 'Temporary password'], r.credentials.map(c => [E(c.roll_no), E(c.email), `<code>${E(c.temp_password)}</code>`])) : '');
  },
  async teacher(fd, f) {
    const b = obj(fd); if (b.capacity_override !== undefined) b.capacity_override = +b.capacity_override;
    const r = await post('/admin/teachers', b); f.reset(); $('#out2').innerHTML = `<p class=err>Teacher created. Temporary password: <code>${E(r.temp_password)}</code> (copy now)</p>`;
  }
};
document.addEventListener('click', async e => { const a = e.target.closest('[data-a]'); if (a) try { await A[a.dataset.a](a); } catch (x) { toast(x.message, 1); } });
document.addEventListener('submit', async e => { const f = e.target.dataset.f; if (!f) return; e.preventDefault(); try { await F[f](new FormData(e.target), e.target); } catch (x) { toast(x.message, 1); } });
document.addEventListener('change', e => { const k = e.target.dataset.flt; if (k) { mf[k] = e.target.value; reload(); } });
boot();
