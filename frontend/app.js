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
          (d.detail_back && d.detail_back.length ? ' ｜ 回溯原文 #' + d.detail_back.join('、') : '') +
          (d.memory_ids && d.memory_ids.length ? ' ｜ 跨会话记忆 M#' + d.memory_ids.join('、M#') : ' ｜ 未命中跨会话记忆'));
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

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
  });
}

var APV = 'pending';

function approvalsView() {
  var tabs = [['pending', '待审批'], ['approved', '已放行'], ['rejected', '已拒绝'], ['all', '全部']];
  $('#view').innerHTML = '<div class="apv">' +
    '<div class="apv-title">飞审 · 审批工作台</div>' +
    '<div class="apv-bar"><b id="apv-n">加载中…</b><span id="apv-role"></span></div>' +
    '<div class="apv-tabs" id="apv-tabs">' + tabs.map(function (t) {
      return '<button data-s="' + t[0] + '"' + (t[0] === APV ? ' class="on"' : '') + '>' + t[1] + '</button>';
    }).join('') + '</div>' +
    '<div id="apv-list" class="apv-list">…</div></div>';

  $('#apv-tabs').onclick = function (e) {
    var b = e.target.closest('button'); if (!b) return;
    APV = b.dataset.s;
    document.querySelectorAll('#apv-tabs button').forEach(function (x) { x.classList.toggle('on', x.dataset.s === APV); });
    load();
  };

  function load() {
    api('approvals?status=' + APV).then(function (d) {
      if (d.brand) { var t = document.querySelector('.apv-title'); if (t) t.textContent = d.brand; }
      $('#apv-n').textContent = (APV === 'pending' ? '待审批 ' + d.rows.length + ' 条' : '共 ' + d.rows.length + ' 条');
      $('#apv-role').textContent = '我的角色：' + d.my_role + ' ｜ 审批人：' + (d.approvers || []).join('、') +
        (d.can_approve ? '（你可放行/拒绝）' : '（只读：你不是审批人）');
      if (!d.rows.length) {
        $('#apv-list').innerHTML = '<div class="apv-empty">这里没有单据。需要审批的动作由 Agent 触发：写操作会在执行前挂起一张单。</div>';
        return;
      }
      $('#apv-list').innerHTML = d.rows.map(function (r) {
        var tag = { pending: '待审批', approved: '已放行', rejected: '已拒绝', expired: '已超时' }[r.status] || r.status;
        var cls = { pending: '', approved: 'ok', rejected: 'bad', expired: 'bad' }[r.status] || '';
        var btns = (r.status === 'pending' && d.can_approve)
          ? '<div class="apv-act"><button class="go" data-id="' + r.id + '" data-d="approved">放行并执行</button>' +
            '<button class="no" data-id="' + r.id + '" data-d="rejected">拒绝</button></div>' : '';
        var result = r.result ? '<div class="apv-res">执行结果：' + esc(String(r.result).slice(0, 300)) + '</div>' : '';
        var meta = (r.decided ? '决策：' + esc(r.decided_by || '') + ' @ ' + esc(r.decided) + (r.note ? '（' + esc(r.note) + '）' : '') : '');
        return '<div class="apv-card" id="apv-c' + r.id + '">' +
          '<div class="apv-h"><b>#' + r.id + ' ' + esc(r.tool) + '</b><span class="tag ' + cls + '">' + tag + '</span></div>' +
          '<div class="apv-kv">发起人 ' + esc(r.user) + ' ｜ 任务 #' + (r.task_id || '-') +
          ' ｜ ' + esc(r.created) + '</div>' +
          (r.goal ? '<div class="apv-goal">任务目标：' + esc(r.goal) + '</div>' : '') +
          '<div class="apv-args">' + esc(JSON.stringify(r.args)) + '</div>' +
          (r.reason ? '<div class="apv-kv">挂起原因：' + esc(r.reason) + '</div>' : '') +
          (meta ? '<div class="apv-kv">' + meta + '</div>' : '') +
          result + '<div class="apv-out" id="apv-o' + r.id + '"></div>' + btns + '</div>';
      }).join('');
    }).catch(function (m) { $('#apv-list').textContent = '加载失败：' + m; });
  }

  $('#apv-list').onclick = function (e) {
    var b = e.target.closest('button[data-id]'); if (!b) return;
    var id = b.dataset.id, decision = b.dataset.d;
    var note = '';
    if (decision === 'rejected') note = prompt('拒绝理由（会回给模型，可留空）：') || '';
    b.disabled = true;
    var out = $('#apv-o' + id);
    if (out) out.textContent = decision === 'approved' ? '放行中，正在执行动作…' : '处理中…';
    api('approvals/decide', { method: 'POST', body: { id: parseInt(id, 10), decision: decision, note: note } })
      .then(function (d) {
        var run = d.run || {};
        var txt = (d.approved ? '✅ 已放行并执行' : '⛔ 已拒绝') +
          (d.result ? '\n执行结果：' + JSON.stringify(d.result).slice(0, 400) : '') +
          (run.task_id ? '\n任务 #' + run.task_id + ' 状态：' + (run.status || (run.answer ? 'done' : '-')) : '') +
          (run.answer ? '\n\n' + run.answer : '');
        if (out) out.textContent = txt;
        setTimeout(load, 600);
      })
      .catch(function (m) {
        if (out) out.textContent = '操作失败：' + m;
        b.disabled = false;
      });
  };

  load();
}

var MEM = 'pending';

function memoryView() {
  var tabs = [['pending', '待确认'], ['active', '已生效'], ['disabled', '已停用'], ['all', '全部']];
  $('#view').innerHTML = '<div class="apv">' +
    '<div class="apv-title">飞忆 · 跨会话长期记忆</div>' +
    '<div class="apv-bar"><b id="mem-n">加载中…</b><span id="mem-c"></span></div>' +
    '<div class="apv-tabs" id="mem-tabs">' + tabs.map(function (t) {
      return '<button data-s="' + t[0] + '"' + (t[0] === MEM ? ' class="on"' : '') + '>' + t[1] + '</button>';
    }).join('') + '<button id="mem-x" class="mem-x">⤓ 从最近会话抽取</button></div>' +
    '<div id="mem-list" class="apv-list">…</div>' +
    '<div class="mem-note">记忆只作参考注入：本轮你明说的内容优先于旧记忆；命中不到就不注入，</div>' +
    '<div class="mem-note2">所以问它没记过的事，它必须说不知道。每条都能停用或删除。</div></div>';

  $('#mem-tabs').onclick = function (e) {
    var b = e.target.closest('button[data-s]'); if (!b) return;
    MEM = b.dataset.s;
    document.querySelectorAll('#mem-tabs button[data-s]').forEach(function (x) {
      x.classList.toggle('on', x.dataset.s === MEM);
    });
    load();
  };

  $('#mem-x').onclick = function () {
    var btn = this; btn.disabled = true; btn.textContent = '抽取中…';
    api('memory/extract', { method: 'POST', body: {} })
      .then(function (d) {
        btn.textContent = '⤓ 已抽 ' + d.created + ' 条（跳过重复 ' + d.skipped + '）';
        MEM = 'pending';
        document.querySelectorAll('#mem-tabs button[data-s]').forEach(function (x) {
          x.classList.toggle('on', x.dataset.s === 'pending');
        });
        load();
        setTimeout(function () { btn.disabled = false; btn.textContent = '⤓ 从最近会话抽取'; }, 2500);
      })
      .catch(function (m) {
        btn.textContent = '抽取失败：' + m;
        btn.disabled = false;
      });
  };

  function load() {
    api('memory?status=' + MEM).then(function (d) {
      if (d.brand) { var t = document.querySelector('.apv-title'); if (t) t.textContent = d.brand; }
      $('#mem-n').textContent = '待确认 ' + d.counts.pending + ' 条 ｜ 已生效 ' + d.counts.active + ' 条';
      $('#mem-c').textContent = '注入上限 ' + d.inject_topk + ' 条/轮 ｜ 我的角色：' + d.my_role;
      if (!d.rows.length) {
        $('#mem-list').innerHTML = '<div class="apv-empty">还没有记忆。在对话里定过事之后，点右上角「从最近会话抽取」，' +
          '抽出来的条目会先放在这里等你确认。</div>';
        return;
      }
      $('#mem-list').innerHTML = d.rows.map(function (r) {
        var kind = { fact: '事实', decision: '决定', preference: '偏好', artifact: '产出物' }[r.kind] || r.kind;
        var tag = { pending: '待确认', active: '已生效', disabled: '已停用' }[r.status] || r.status;
        var cls = { pending: '', active: 'ok', disabled: 'bad' }[r.status] || '';
        var src = '来源：会话 #' + (r.src_conv || '-') + ' ｜ ' + String(r.src_time || r.created).slice(0, 16) +
          ' ｜ 置信度 ' + r.confidence + ' ｜ 命中 ' + (r.hits || 0) + ' 次';
        var btns = '';
        if (r.status === 'pending') {
          btns = '<div class="apv-act"><button class="go" data-id="' + r.id + '" data-a="activate">记住</button>' +
            '<button class="no" data-id="' + r.id + '" data-a="delete">忽略</button></div>';
        } else if (r.status === 'active') {
          btns = '<div class="apv-act"><button class="no" data-id="' + r.id + '" data-a="disable">停用</button></div>';
        } else {
          btns = '<div class="apv-act"><button class="go" data-id="' + r.id + '" data-a="activate">启用</button>' +
            '<button class="no" data-id="' + r.id + '" data-a="delete">删除</button></div>';
        }
        return '<div class="apv-card mem-card">' +
          '<div class="apv-h"><b>M#' + r.id + ' ' + esc(r.text) + '</b><span class="tag ' + cls + '">' + tag + '</span></div>' +
          '<div class="apv-kv">' + kind + ' ｜ ' + src + '</div>' +
          '<div class="apv-out" id="mem-o' + r.id + '"></div>' + btns + '</div>';
      }).join('');
    }).catch(function (m) { $('#mem-list').textContent = '加载失败：' + m; });
  }

  $('#mem-list').onclick = function (e) {
    var b = e.target.closest('button[data-id]'); if (!b) return;
    var id = parseInt(b.dataset.id, 10), action = b.dataset.a;
    b.disabled = true;
    var out = $('#mem-o' + id);
    if (out) out.textContent = '处理中…';
    api('memory/decide', { method: 'POST', body: { id: id, action: action } })
      .then(function () { load(); })
      .catch(function (m) {
        if (out) out.textContent = '操作失败：' + m;
        b.disabled = false;
      });
  };

  load();
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
  else if (CUR === 'approvals') approvalsView();
  else if (CUR === 'memory') memoryView();
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
