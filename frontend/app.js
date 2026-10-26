var TOKEN = localStorage.getItem('feinao_token') || '';
var ROLE = localStorage.getItem('feinao_role') || 'viewer';
var CONV = parseInt(localStorage.getItem('feinao_conv') || '0', 10) || 0;
var CUR = 'chat';
var $ = function (s) { return document.querySelector(s); };

function api(path, opt) {
  opt = opt || {};
  opt.headers = { 'Content-Type': 'application/json' };
  if (TOKEN) opt.headers['Authorization'] = 'Bearer ' + TOKEN;
  if (opt.body) opt.body = JSON.stringify(opt.body);
  return fetch('api/' + path, opt).then(function (r) {
    return r.json().then(function (j) { return { status: r.status, j: j }; });
  }).then(function (o) {
    if (o.status === 401) { logout(); throw '请先登录'; }
    if (o.status >= 400) throw (o.j && (o.j.error || o.j.message)) || ('HTTP ' + o.status);
    return o.j;
  });
}

function logout() { TOKEN = ''; localStorage.removeItem('feinao_token'); location.reload(); }

function bubble(cls, txt) {
  var d = document.createElement('div');
  d.className = 'msg ' + cls; d.textContent = txt;
  var box = $('#msgs');
  box.appendChild(d); box.scrollTop = box.scrollHeight;
  return d;
}

function chatView() {
  $('#view').innerHTML = '<div class="chat">' +
    '<div class="cbar"><select id="csel"></select><button id="cnew">新会话</button>' +
    '<span id="cstat" class="cstat">长期记忆：开</span></div>' +
    '<div id="msgs" class="msgs">' +
    '<div class="msg ai">你好，我是飞脑。本会话有长期记忆：聊过的事我会一直记得，摘要装不下的细节还能回溯原文。</div></div>' +
    '<div class="inp"><textarea id="itx" placeholder="输入问题，Enter 发送"></textarea><button id="isend">发送</button></div></div>';
  $('#isend').onclick = send;
  $('#itx').onkeydown = function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  $('#cnew').onclick = function () { CONV = 0; localStorage.removeItem('feinao_conv'); $('#msgs').innerHTML = ''; renderConvs(); };
  $('#csel').onchange = function () { CONV = parseInt(this.value, 10) || 0; if (CONV) localStorage.setItem('feinao_conv', CONV); renderConvs(); };
  renderConvs();

  function renderConvs() {
    api('lcm_convs').then(function (d) {
      var rows = d.rows || [];
      if (!CONV && rows.length) { CONV = rows[0].id; localStorage.setItem('feinao_conv', CONV); }
      var sel = $('#csel'); if (!sel) return;
      sel.innerHTML = '<option value="0">＋ 新会话</option>' + rows.map(function (r) {
        return '<option value="' + r.id + '"' + (r.id === CONV ? ' selected' : '') + '>#' + r.id +
          ' ' + (r.title || '') .slice(0, 16) + '（' + r.turns + ' 轮）</option>';
      }).join('');
      if (CONV) { sel.value = String(CONV); loadHistory(); }
    }).catch(function () {});
  }

  function loadHistory() {
    api('lcm?conv_id=' + CONV).then(function (d) {
      var box = $('#msgs'); if (!box) return;
      box.innerHTML = '<div class="msg ai">历史会话 #' + d.conv_id + '：' + d.turns +
        ' 轮 / 原始 ' + d.raw_tokens + ' token，本轮上下文预算 ' + d.budget + ' token。</div>';
      updateStat(d);
    }).catch(function () {});
  }

  function updateStat(st) {
    var s = $('#cstat'); if (!s || !st) return;
    s.textContent = '会话 #' + st.conv_id + ' ｜ ' + st.turns + ' 轮 ｜ 上下文 ' + st.context_tokens +
      '/' + st.budget + ' token ｜ 摘要 ' + st.nodes + ' 段';
  }

  function send() {
    var t = $('#itx').value.trim(); if (!t) return;
    $('#itx').value = '';
    bubble('me', t);
    var wait = bubble('ai', '思考中…');
    var body = { text: t };
    if (CONV) body.conv_id = CONV;
    api('chat', { method: 'POST', body: body })
      .then(function (d) {
        CONV = d.conv_id; localStorage.setItem('feinao_conv', CONV);
        wait.textContent = d.answer;
        bubble('meta', '—— ' + d.provider + ' ｜ 上下文 ' + d.context_tokens + ' token' +
          (d.summaries ? ' ｜ 摘要 ' + d.summaries + ' 段' : '') +
          (d.detail_back && d.detail_back.length ? ' ｜ 回溯原文 #' + d.detail_back.join('、') : ''));
        updateStat(d.stats);
        renderConvs();
      })
      .catch(function (m) { wait.textContent = '出错了：' + m; });
  }
}

function pilotsView() {
  $('#view').innerHTML = '<div class="pilots">' +
    '<div class="pilot"><h3>📚 知识问答</h3><textarea id="p1" placeholder="问一个知识库内的问题…">飞系系统怎么部署？</textarea><button id="b1">提问</button><div class="out" id="o1"></div></div>' +
    '<div class="pilot"><h3>📊 智能查数</h3><textarea id="p2" placeholder="用自然语言查飞瞰BI的数据…">查一下商品数据</textarea><button id="b2">查询</button><div class="out" id="o2"></div></div>' +
    '<div class="pilot"><h3>💬 客服话术</h3><textarea id="p3" placeholder="描述客服场景…">客户嫌价格贵，如何应对</textarea><button id="b3">生成</button><div class="out" id="o3"></div></div>' +
    '</div>';
  $('#b1').onclick = function () {
    $('#o1').textContent = '检索中…';
    api('ai/ask', { method: 'POST', body: { q: $('#p1').value } })
      .then(function (d) { $('#o1').textContent = d.answer + '\n\n来源：' + (d.sources || []).map(function (s) { return '#' + s.id + ' ' + s.title; }).join('、'); })
      .catch(function (m) { $('#o1').textContent = m; });
  };
  $('#b2').onclick = function () {
    $('#o2').textContent = '查询中…';
    api('ai/nl2api', { method: 'POST', body: { q: $('#p2').value, system: 'bi' } })
      .then(function (d) { $('#o2').textContent = d.error || ('实体：' + d.entity + '（' + d.rows + ' 条）\n\n' + d.summary); })
      .catch(function (m) { $('#o2').textContent = m; });
  };
  $('#b3').onclick = function () {
    $('#o3').textContent = '生成中…';
    api('ai/script', { method: 'POST', body: { scenario: $('#p3').value, tone: '专业友好' } })
      .then(function (d) { $('#o3').textContent = d.script; })
      .catch(function (m) { $('#o3').textContent = m; });
  };
}

function auditView() {
  $('#view').innerHTML = '<div class="kpis" id="kpis"></div><div id="tbl">加载中…</div>';
  api('dashboard').then(function (d) {
    $('#kpis').innerHTML = Object.keys(d.counts).map(function (k) {
      return '<div class="kpi"><b>' + d.counts[k] + '</b><span>' + k + '</span></div>';
    }).join('') + '<div class="kpi"><b>' + d.llm_ok + '/' + d.llm_fail + '</b><span>LLM 成功/失败</span></div>';
  });
  api('audit').then(function (d) {
    $('#tbl').innerHTML = '<table><tr><th>时间</th><th>用户</th><th>工具</th><th>参数</th><th>结果</th><th>状态</th></tr>' +
      (d.rows || []).map(function (r) {
        return '<tr><td>' + r.ts + '</td><td>' + r.user + '</td><td>' + r.tool + '</td><td>' +
          (r.args || '').slice(0, 60) + '</td><td>' + (r.result || '').slice(0, 60) + '</td><td><span class="tag ' +
          (r.ok ? 'ok">成功' : 'bad">失败') + '</span></td></tr>';
      }).join('') + '</table>';
  }).catch(function (m) { $('#tbl').textContent = m; });
}

function render() {
  document.querySelectorAll('#nav a').forEach(function (a) { a.classList.toggle('on', a.dataset.k === CUR); });
  if (CUR === 'chat') chatView();
  else if (CUR === 'pilots') pilotsView();
  else auditView();
}

$('#nav').onclick = function (e) {
  var a = e.target.closest('a'); if (!a) return;
  CUR = a.dataset.k; render();
};

$('#lgf').onsubmit = function (e) {
  e.preventDefault();
  api('login', { method: 'POST', body: { username: $('#lg-u').value, password: $('#lg-p').value } })
    .then(function (d) {
      TOKEN = d.token; ROLE = d.role;
      localStorage.setItem('feinao_token', TOKEN);
      localStorage.setItem('feinao_role', ROLE);
      enter();
    })
    .catch(function (m) { $('#lg-err').textContent = m; });
};

function enter() {
  $('#lg').hidden = true; $('#main').hidden = false;
  $('#who').textContent = ROLE;
  if (ROLE === 'viewer') { document.querySelector('#nav a[data-k=audit]').style.display = 'none'; }
  render();
}

if (TOKEN) enter();
