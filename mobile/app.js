var CFG = window.APP_CONFIG;
var TOKEN = localStorage.getItem('feinao_m_token') || '';
var MODE = 'chat';
var $ = function (s) { return document.querySelector(s); };

function api(path, opt) {
  opt = opt || {};
  opt.headers = { 'Content-Type': 'application/json' };
  if (TOKEN) opt.headers['Authorization'] = 'Bearer ' + TOKEN;
  if (opt.body) opt.body = JSON.stringify(opt.body);
  return fetch(CFG.base + 'api/' + path, opt).then(function (r) {
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (o) {
    if (o.status === 401) { TOKEN=''; localStorage.removeItem('feinao_m_token'); location.reload(); throw '请先登录'; }
    if (o.status >= 400) throw (o.j && (o.j.error || o.j.message)) || ('HTTP ' + o.status);
    return o.j;
  });
}

function bubble(cls, txt) {
  var d = document.createElement('div');
  d.className = 'msg ' + cls; d.textContent = txt;
  var box = $('#msgs');
  box.appendChild(d); box.scrollTop = box.scrollHeight;
  return d;
}

var ROUTES = {
  chat: function (t) {
    return api('chat', { method: 'POST', body: { messages: [{ role: 'user', content: t }] } })
      .then(function (d) { return d.answer; });
  },
  ask: function (t) {
    bubble('me', '📚 ' + t);
    var w = bubble('ai', '检索知识库中…');
    return api('ai/ask', { method: 'POST', body: { q: t } }).then(function (d) {
      w.textContent = d.answer + '\n\n来源：' + (d.sources || []).map(function (s) { return s.title; }).join('、');
      return null;
    });
  },
  nl2api: function (t) {
    bubble('me', '📊 ' + t);
    var w = bubble('ai', '查询中…');
    return api('ai/nl2api', { method: 'POST', body: { q: t, system: 'bi' } }).then(function (d) {
      w.textContent = d.error || ('实体：' + d.entity + '（' + d.rows + ' 条）\n\n' + d.summary);
      return null;
    });
  },
  script: function (t) {
    bubble('me', '💬 ' + t);
    var w = bubble('ai', '生成话术中…');
    return api('ai/script', { method: 'POST', body: { scenario: t, tone: '专业友好' } }).then(function (d) {
      w.textContent = d.script;
      return null;
    });
  }
};

$('#isend').onclick = send;
$('#itx').onkeydown = function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
function send() {
  var t = $('#itx').value.trim(); if (!t) return;
  $('#itx').value = '';
  if (MODE === 'chat') bubble('me', t);
  var w = MODE === 'chat' ? bubble('ai', '思考中…') : null;
  ROUTES[MODE](t).then(function (ans) { if (w && ans) w.textContent = ans; })
    .catch(function (m) { if (w) w.textContent = '出错了：' + m; });
}

document.querySelector('.chips').onclick = function (e) {
  var a = e.target.closest('a'); if (!a) return;
  MODE = a.dataset.p;
  document.querySelectorAll('.chips a').forEach(function (x) { x.classList.toggle('on', x === a); });
  $('#pg').textContent = a.textContent.trim();
  $('#itx').placeholder = MODE === 'chat' ? '输入…' : a.textContent.trim() + '：输入内容…';
};

$('#lgf').onsubmit = function (e) {
  e.preventDefault();
  api('login', { method: 'POST', body: { username: $('#lg-u').value, password: $('#lg-p').value } })
    .then(function (d) { TOKEN = d.token; localStorage.setItem('feinao_m_token', TOKEN); enter(); })
    .catch(function (m) { $('#lg-err').textContent = m; });
};
function enter() { $('#lg').hidden = true; $('#main').hidden = false; bubble('ai', '你好，我是飞脑。上方可切换：知识问答 / 智能查数 / 客服话术 / 自由对话。'); }
if (TOKEN) enter();
