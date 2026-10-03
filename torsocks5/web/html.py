"""Web 面板页面模板（字符串返回，零构建：CDN 引入 Tailwind / Alpine / htmx）。

约定：页面只负责骨架与样式，数据全部由前端 fetch 同源 JSON API 获取
（Alpine 的 ``x-text`` 默认转义，避免把日志/配置内容注入成 HTML）。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

#: 导航项：(路径, 标题)
NAV: List[Tuple[str, str]] = [
    ("/", "仪表盘"),
    ("/routes", "路由"),
    ("/bridges", "网桥"),
    ("/config", "配置"),
    ("/logs", "日志"),
]

_TAILWIND = '<script src="https://cdn.tailwindcss.com"></script>'
_ALPINE = ('<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>')
_HTMX = '<script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>'


def _shell(title: str, active: str, body: str, extra_scripts: str = "") -> str:
    links = []
    for href, label in NAV:
        current = " bg-slate-700 text-white" if href == active else \
            " text-slate-300 hover:bg-slate-700 hover:text-white"
        links.append(
            '<a href="%s" class="rounded-md px-3 py-2 text-sm font-medium%s">%s</a>'
            % (href, current, label)
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title} · TorSOCKS5</title>
{_TAILWIND}
{_HTMX}
{_ALPINE}
{extra_scripts}
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen">
<header class="bg-slate-800 border-b border-slate-700">
  <div class="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-lg font-bold tracking-tight">TorSOCKS5</span>
      <span class="text-xs text-slate-400 hidden sm:inline">本机管理面板</span>
    </div>
    <nav class="flex items-center gap-1">{''.join(links)}<a href="/wizard" class="ml-2 rounded-md bg-indigo-600 hover:bg-indigo-500 px-3 py-2 text-sm font-medium text-white">⚙ 配置向导</a></nav>
  </div>
</header>
<main class="mx-auto max-w-6xl px-4 py-6">
{body}
</main>
<footer class="mx-auto max-w-6xl px-4 py-6 text-xs text-slate-500">
  TorSOCKS5 · 只监听本机 · 改动配置后可通过面板保存并热重载
</footer>
</body>
</html>"""


# --------------------------------------------------------------------- 仪表盘
def dashboard_page() -> str:
    body = """
<div x-data="dash()" x-init="start()">
  <div class="flex items-baseline justify-between mb-4">
    <h1 class="text-xl font-bold">仪表盘</h1>
    <span class="text-xs text-slate-400" x-text="d ? ('更新于 ' + d.now) : '加载中…'"></span>
  </div>

  <template x-if="d && d.update && d.update.has_update">
    <div class="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-700 bg-amber-900/50 px-4 py-3 text-sm text-amber-200">
      <span>发现新版本 <b x-text="d.update.latest"></b>（当前 <span x-text="d.update.current"></span>）</span>
      <a :href="d.update.url" target="_blank" rel="noopener" class="underline hover:text-amber-100">查看更新 →</a>
    </div>
  </template>

  <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400">当前连接</div>
      <div class="text-2xl font-bold" x-text="d ? d.proxy.current_connections : '-'"></div>
      <div class="text-xs text-slate-500" x-text="d ? ('累计 ' + d.proxy.total_connections + ' / 上限 ' + d.proxy.max_connections) : ''"></div>
    </div>
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400">上行 ↑</div>
      <div class="text-2xl font-bold" x-text="d ? fmt(d.proxy.bytes_up) : '-'"></div>
      <div class="text-xs text-slate-500">下行 ↓ <span x-text="d ? fmt(d.proxy.bytes_down) : '-'"></span></div>
    </div>
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400">运行时长</div>
      <div class="text-2xl font-bold" x-text="d ? dur(d.proxy.uptime) : '-'"></div>
      <div class="text-xs text-slate-500" x-text="d ? ('版本 ' + d.version) : ''"></div>
    </div>
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400">网桥</div>
      <div class="text-2xl font-bold" x-text="d ? d.bridge_count : '-'"></div>
      <div class="text-xs text-slate-500">已启用条数</div>
    </div>
  </div>

  <div class="mt-6 grid md:grid-cols-2 gap-4">
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400 mb-1">路由</div>
      <div class="text-lg font-semibold" x-text="d ? d.route.name : '-'"></div>
      <div class="text-sm text-slate-300" x-text="d ? d.route.title : ''"></div>
      <div class="mt-2 text-xs text-slate-400 whitespace-pre-wrap" x-text="d ? d.route.status : ''"></div>
    </div>
    <div class="rounded-lg bg-slate-800 p-4">
      <div class="text-xs text-slate-400 mb-1">热重载</div>
      <template x-if="d && d.hotreload">
        <div class="text-sm space-y-1">
          <div>状态：<span class="text-emerald-400" x-text="d.hotreload.enabled ? '已启用' : '已关闭'"></span>
               · 信号 <span x-text="d.hotreload.signal"></span>
               · 已重载 <span x-text="d.hotreload.reload_count"></span> 次</div>
          <div class="text-xs text-slate-400" x-text="'API: ' + (d.hotreload.api_enabled ? '开' : '关') + (d.hotreload.last_error ? (' · 最近错误: ' + d.hotreload.last_error) : '')"></div>
        </div>
      </template>
      <template x-if="d && !d.hotreload"><div class="text-sm text-slate-500">未启用</div></template>
      <div class="mt-3 flex gap-2">
        <button @click="reload()" class="rounded bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 text-sm">触发配置重载</button>
        <span class="text-xs text-slate-400 self-center" x-text="reloadMsg"></span>
      </div>
    </div>
  </div>

  <div class="mt-6 rounded-lg bg-slate-800 p-4">
    <div class="text-xs text-slate-400 mb-2">分流统计</div>
    <template x-if="d && d.split">
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 text-sm">
        <div>
          <div class="text-xs text-slate-500">模式</div>
          <div class="font-semibold" x-text="{off:'关闭',smart:'智能',auto:'自动',all:'全量'}[d.split.mode] || d.split.mode"></div>
          <div class="text-xs text-slate-500" x-text="d.split.requested && d.split.requested !== d.split.mode ? ('请求 ' + d.split.requested) : ''"></div>
        </div>
        <div>
          <div class="text-xs text-slate-500">走隧道</div>
          <div class="font-semibold text-emerald-400" x-text="d.split.hits_tunnel + ' 次'"></div>
        </div>
        <div>
          <div class="text-xs text-slate-500">直连</div>
          <div class="font-semibold text-sky-400" x-text="d.split.hits_direct + ' 次'"></div>
        </div>
        <div>
          <div class="text-xs text-slate-500">私有地址</div>
          <div class="font-semibold" x-text="d.split.hits_private + ' 次'"></div>
        </div>
        <div>
          <div class="text-xs text-slate-500">代理规则</div>
          <div class="font-semibold" x-text="d.split.proxy_hosts + ' 条'"></div>
        </div>
        <div>
          <div class="text-xs text-slate-500">直连规则</div>
          <div class="font-semibold" x-text="d.split.direct_hosts + ' 条'"></div>
        </div>
      </div>
    </template>
    <template x-if="!d || !d.split"><div class="text-sm text-slate-500">-</div></template>
  </div>
</div>
<script>
function dash() {
  return {
    d: null, reloadMsg: '',
    async load() {
      try {
        const r = await fetch('/api/status');
        if (r.ok) { this.d = await r.json(); this.d.now = new Date().toLocaleTimeString(); }
      } catch (e) { /* 重试由定时器负责 */ }
    },
    start() { this.load(); setInterval(() => this.load(), 2000); },
    async reload() {
      this.reloadMsg = '重载中…';
      try {
        const r = await fetch('/api/reload', {method: 'POST'});
        const j = await r.json();
        this.reloadMsg = j.success ? ('成功（' + (j.changes ? j.changes.length : 0) + ' 项变更）')
                                   : ('失败: ' + (j.error || '未知错误'));
      } catch (e) { this.reloadMsg = '请求失败'; }
    },
    fmt(n) {
      if (n == null) return '-';
      const units = ['B','KB','MB','GB','TB']; let i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      return (i ? n.toFixed(1) : n) + ' ' + units[i];
    },
    dur(s) {
      if (!s && s !== 0) return '-';
      s = Math.floor(s);
      const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
            m = Math.floor(s % 3600 / 60), sec = s % 60;
      if (d) return d + '天' + h + '时';
      if (h) return h + '时' + m + '分';
      if (m) return m + '分' + sec + '秒';
      return sec + '秒';
    }
  }
}
</script>
"""
    return _shell("仪表盘", "/", body)


# --------------------------------------------------------------------- 路由
def routes_page() -> str:
    body = """
<div x-data="routes()" x-init="load()">
  <div class="flex items-baseline justify-between mb-4">
    <h1 class="text-xl font-bold">路由</h1>
    <button @click="load()" class="text-xs text-slate-400 hover:text-white">刷新</button>
  </div>

  <div class="rounded-lg bg-slate-800 p-4 mb-4" x-show="data">
    <div class="text-xs text-slate-400">当前路由</div>
    <div class="text-lg font-semibold" x-text="data ? data.current : '-'"></div>
    <div class="text-sm text-slate-300 whitespace-pre-wrap" x-text="data ? data.status : ''"></div>
    <template x-if="data && data.stats">
      <div class="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-400">
        <span>负载均衡 <span class="text-slate-200" x-text="data.stats.strategy || '-'"></span></span>
        <span>熔断 <span class="px-1.5 rounded"
            :class="data.stats.circuit_breaker === 'healthy' ? 'bg-emerald-900 text-emerald-300' : 'bg-rose-900 text-rose-300'"
            x-text="data.stats.circuit_breaker || '-'"></span></span>
        <span>节点健康 <span class="text-slate-200" x-text="(data.stats.healthy_nodes ?? '-') + ' / ' + (data.stats.total_nodes ?? '-')"></span></span>
        <span x-show="data.stats.split">分流 <span class="text-slate-200" x-text="data.stats.split ? data.stats.split.mode : ''"></span></span>
      </div>
    </template>
  </div>

  <div class="rounded-lg bg-slate-800 p-4 mb-4">
    <div class="text-xs text-slate-400 mb-2">切换路由（运行期热切换，不写入配置文件）</div>
    <div class="flex flex-wrap gap-2">
      <template x-for="r in (data ? data.available : [])" :key="r[0]">
        <button @click="switchTo(r[0])"
          class="rounded-md px-3 py-1.5 text-sm border"
          :class="data && data.current === r[0] ? 'bg-indigo-600 border-indigo-500' : 'bg-slate-700 border-slate-600 hover:bg-slate-600'"
          x-text="r[0] + (data && data.current === r[0] ? '（当前）' : '')"></button>
      </template>
    </div>
    <div class="mt-2 text-xs" :class="switchOk ? 'text-emerald-400' : 'text-rose-400'" x-text="switchMsg"></div>
    <div class="mt-1 text-xs text-slate-500" x-show="data && !data.switchable">热重载未启用，无法在线切换。</div>
  </div>

  <div class="rounded-lg bg-slate-800 p-4" x-show="data && data.nodes && data.nodes.length">
    <div class="text-xs text-slate-400 mb-2">中继节点（多中继负载均衡）</div>
    <table class="w-full text-sm">
      <thead><tr class="text-left text-xs text-slate-400">
        <th class="py-1">节点</th><th>状态</th><th>并发</th><th>权重</th><th>成功率</th><th>延迟</th>
      </tr></thead>
      <tbody>
      <template x-for="(n, i) in (data ? data.nodes : [])" :key="i">
        <tr class="border-t border-slate-700">
          <td class="py-1.5 font-mono text-xs" x-text="n.url"></td>
          <td><span class="px-1.5 rounded text-xs"
              :class="n.state === 'healthy' ? 'bg-emerald-900 text-emerald-300' : 'bg-rose-900 text-rose-300'"
              x-text="n.state"></span></td>
          <td x-text="n.active_streams"></td>
          <td x-text="n.weight"></td>
          <td x-text="(n.success_rate * 100).toFixed(1) + '%'"></td>
          <td x-text="n.avg_latency_ms + ' ms'"></td>
        </tr>
      </template>
      </tbody>
    </table>
  </div>
</div>
<script>
function routes() {
  return {
    data: null, switchMsg: '', switchOk: true,
    async load() {
      try {
        const r = await fetch('/api/routes');
        if (r.ok) this.data = await r.json();
      } catch (e) {}
    },
    async switchTo(name) {
      this.switchMsg = '切换中…'; this.switchOk = true;
      try {
        const r = await fetch('/api/routes/switch', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({route: name})
        });
        const j = await r.json();
        this.switchOk = !!j.success;
        this.switchMsg = j.success ? ('已切换到 ' + name) : ('失败: ' + (j.error || '未知错误'));
        this.load();
      } catch (e) { this.switchOk = false; this.switchMsg = '请求失败'; }
    }
  }
}
</script>
"""
    return _shell("路由", "/routes", body)


# --------------------------------------------------------------------- 网桥
def bridges_page() -> str:
    body = """
<div x-data="bridges()" x-init="load()">
  <div class="flex items-baseline justify-between mb-4">
    <h1 class="text-xl font-bold">网桥</h1>
    <button @click="load()" class="text-xs text-slate-400 hover:text-white">刷新</button>
  </div>

  <div class="rounded-lg bg-slate-800 p-4 mb-4">
    <div class="text-xs text-slate-400 mb-2">添加网桥行</div>
    <div class="flex gap-2">
      <input x-model="newLine" @keydown.enter="add()"
        class="flex-1 rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm font-mono"
        placeholder="Bridge meek 0.0.2.0:3 url=... front=...">
      <button @click="add()" class="rounded bg-indigo-600 hover:bg-indigo-500 px-4 py-2 text-sm">添加</button>
    </div>
    <div class="mt-2 text-xs" :class="msgOk ? 'text-emerald-400' : 'text-rose-400'" x-text="msg"></div>
  </div>

  <div class="rounded-lg bg-slate-800 p-4">
    <div class="text-xs text-slate-400 mb-2"
         x-text="'已配置 ' + (data ? data.bridges.length : 0) + ' 条（启用 ' + (data ? data.active : 0) + '）'"></div>
    <div class="overflow-auto">
      <table class="w-full text-sm">
        <thead><tr class="text-left text-xs text-slate-400">
          <th class="py-1">#</th><th>传输</th><th>地址</th><th>指纹</th><th></th>
        </tr></thead>
        <tbody>
        <template x-for="(b, i) in (data ? data.bridges : [])" :key="i">
          <tr class="border-t border-slate-700">
            <td class="py-1.5" x-text="i + 1"></td>
            <td><span class="px-1.5 rounded bg-slate-700 text-xs" x-text="b.transport"></span></td>
            <td class="font-mono text-xs" x-text="b.address"></td>
            <td class="font-mono text-xs text-slate-400" x-text="(b.fingerprint || '').slice(0, 16)"></td>
            <td class="text-right">
              <button @click="remove(b.address)" class="text-rose-400 hover:text-rose-300 text-xs">删除</button>
            </td>
          </tr>
        </template>
        </tbody>
      </table>
      <div class="text-sm text-slate-500 py-4" x-show="data && !data.bridges.length">
        还没有网桥。可用 <code class="font-mono">torsocks5 bridges fetch</code> 获取，或到
        <a class="text-indigo-400 underline" href="https://bridges.torproject.org" target="_blank" rel="noopener">bridges.torproject.org</a> 复制。
      </div>
    </div>
  </div>
</div>
<script>
function bridges() {
  return {
    data: null, newLine: '', msg: '', msgOk: true,
    async load() {
      try {
        const r = await fetch('/api/bridges');
        if (r.ok) this.data = await r.json();
      } catch (e) {}
    },
    async post(body) {
      try {
        const r = await fetch('/api/bridges', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const j = await r.json();
        this.msgOk = !!j.success;
        this.msg = j.success ? (j.message || '成功') : ('失败: ' + (j.error || '未知错误'));
        this.load();
      } catch (e) { this.msgOk = false; this.msg = '请求失败'; }
    },
    add() {
      if (!this.newLine.trim()) return;
      this.post({action: 'add', line: this.newLine.trim()});
      this.newLine = '';
    },
    remove(needle) { this.post({action: 'rm', needle: needle}); }
  }
}
</script>
"""
    return _shell("网桥", "/bridges", body)


# --------------------------------------------------------------------- 配置
def config_page() -> str:
    body = """
<div x-data="cfg()" x-init="load()">
  <div class="flex items-baseline justify-between mb-4">
    <h1 class="text-xl font-bold">配置</h1>
    <span class="text-xs text-slate-400 font-mono" x-text="data ? data.path : ''"></span>
  </div>

  <div class="rounded-lg bg-slate-800 p-4">
    <textarea x-model="content" spellcheck="false"
      class="w-full h-96 rounded bg-slate-900 border border-slate-600 p-3 text-sm font-mono text-slate-200"
      placeholder="加载中…"></textarea>
    <div class="mt-3 flex items-center gap-2">
      <button @click="save()" class="rounded bg-indigo-600 hover:bg-indigo-500 px-4 py-2 text-sm">保存并热重载</button>
      <button @click="load()" class="rounded bg-slate-700 hover:bg-slate-600 px-4 py-2 text-sm">放弃修改</button>
      <span class="text-xs" :class="msgOk ? 'text-emerald-400' : 'text-rose-400'" x-text="msg"></span>
    </div>
    <div class="mt-2 text-xs text-slate-500">
      保存前会做 TOML 语法校验；校验失败不会写入文件。保存成功后自动触发热重载（若已启用）。
    </div>
  </div>
</div>
<script>
function cfg() {
  return {
    data: null, content: '', msg: '', msgOk: true,
    async load() {
      try {
        const r = await fetch('/api/config');
        if (r.ok) { this.data = await r.json(); this.content = this.data.content; this.msg = ''; }
      } catch (e) { this.msg = '加载失败'; this.msgOk = false; }
    },
    async save() {
      try {
        const r = await fetch('/api/config', {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({content: this.content})
        });
        const j = await r.json();
        this.msgOk = !!j.success;
        this.msg = j.success ? ('已保存' + (j.reloaded ? ' 并重载' : '')) : ('失败: ' + (j.error || ''));
      } catch (e) { this.msgOk = false; this.msg = '请求失败'; }
    }
  }
}
</script>
"""
    return _shell("配置", "/config", body)


# --------------------------------------------------------------------- 日志
def logs_page() -> str:
    body = """
<div x-data="logs()" x-init="boot()">
  <div class="flex items-center justify-between mb-4">
    <h1 class="text-xl font-bold">日志</h1>
    <div class="flex items-center gap-3 text-sm">
      <label class="flex items-center gap-1 text-slate-400 text-xs">
        <input type="checkbox" x-model="paused"> 暂停滚动
      </label>
      <button @click="entries = []" class="text-xs text-slate-400 hover:text-white">清屏</button>
      <span class="text-xs text-slate-500" x-text="connected ? '已连接' : '重连中…'"
            :class="connected ? 'text-emerald-400' : 'text-rose-400'"></span>
    </div>
  </div>

  <div class="rounded-lg bg-slate-900 border border-slate-700 p-3 h-[70vh] overflow-auto font-mono text-xs leading-5"
       x-ref="box">
    <template x-for="e in entries" :key="e.seq">
      <div class="whitespace-pre-wrap">
        <span class="text-slate-500" x-text="e.ts"></span>
        <span class="px-1"
          :class="{'text-sky-400': e.level === 'debug', 'text-slate-300': e.level === 'info',
                   'text-amber-400': e.level === 'warn', 'text-rose-400': e.level === 'error'}"
          x-text="e.level.toUpperCase()"></span>
        <span class="text-slate-200" x-text="e.msg"></span>
      </div>
    </template>
    <div class="text-slate-500" x-show="!entries.length">等待日志…</div>
  </div>
</div>
<script>
function logs() {
  return {
    entries: [], connected: false, paused: false, es: null,
    async boot() {
      try {
        const r = await fetch('/api/logs?limit=300');
        if (r.ok) { const j = await r.json(); this.entries = j.entries || []; this.scroll(); }
      } catch (e) {}
      this.connect();
    },
    connect() {
      this.es = new EventSource('/api/logs/stream');
      this.es.onopen = () => { this.connected = true; };
      this.es.onmessage = (ev) => {
        try {
          const e = JSON.parse(ev.data);
          this.entries.push(e);
          if (this.entries.length > 2000) this.entries.splice(0, 500);
          this.scroll();
        } catch (err) {}
      };
      this.es.onerror = () => { this.connected = false; };
    },
    scroll() {
      if (this.paused) return;
      this.$nextTick(() => { const b = this.$refs.box; if (b) b.scrollTop = b.scrollHeight; });
    }
  }
}
</script>
"""
    return _shell("日志", "/logs", body)


# --------------------------------------------------------------------- 配置向导
def wizard_page() -> str:
    body = """
<div x-data="wizard()" x-init="init()">
  <div class="flex items-baseline justify-between mb-4">
    <h1 class="text-xl font-bold">配置向导</h1>
    <span class="text-xs text-slate-400">5 步完成配置 · 随时可返回仪表盘</span>
  </div>

  <!-- 进度条 -->
  <div class="mb-6 flex flex-wrap items-center gap-2 text-xs">
    <template x-for="(s, i) in steps" :key="i">
      <div class="flex items-center gap-2">
        <span class="flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold"
          :class="i + 1 === step ? 'bg-indigo-500 text-white' : (i + 1 < step ? 'bg-emerald-600 text-white' : 'bg-slate-700 text-slate-400')"
          x-text="i + 1 < step ? '✓' : i + 1"></span>
        <span :class="i + 1 === step ? 'text-white font-medium' : 'text-slate-400'" x-text="s"></span>
        <span class="text-slate-600" x-show="i < steps.length - 1">→</span>
      </div>
    </template>
  </div>

  <!-- 第 1 步：环境检测 -->
  <section x-show="step === 1" class="rounded-lg bg-slate-800 p-5">
    <div class="flex items-center justify-between mb-3">
      <h2 class="font-semibold">① 环境检测</h2>
      <button @click="loadEnv()" class="text-xs text-slate-400 hover:text-white"
        x-text="envLoading ? '检测中…' : '重新检测'"></button>
    </div>
    <p class="mb-3 text-xs text-slate-400">先检查运行环境是否就绪；每一项不通过都会给出处理建议。</p>
    <div x-show="envError" class="mb-3 rounded bg-rose-900/50 px-3 py-2 text-sm text-rose-300" x-text="envError"></div>
    <div class="space-y-2" x-show="env && !envLoading">
      <template x-for="c in env.checks" :key="c.key">
        <div class="flex items-start gap-3 rounded bg-slate-700/50 px-3 py-2">
          <span class="mt-0.5 font-bold" :class="c.ok ? 'text-emerald-400' : 'text-amber-400'" x-text="c.ok ? '✓' : '!'"></span>
          <div class="flex-1 min-w-0">
            <div class="text-sm font-medium" x-text="c.label"></div>
            <div class="text-xs text-slate-400 break-all" x-text="c.detail"></div>
            <div class="text-xs text-amber-300 mt-0.5" x-show="!c.ok && c.fix" x-text="'建议：' + c.fix"></div>
          </div>
        </div>
      </template>
    </div>
    <div class="mt-3 text-sm text-slate-400" x-show="envLoading">正在检测环境…（约 1 秒）</div>
    <div class="mt-4 flex justify-end">
      <button @click="next()" class="rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-4 py-2 text-sm font-medium"
        :disabled="!env || envLoading">下一步：基础配置</button>
    </div>
  </section>

  <!-- 第 2 步：基础配置 -->
  <section x-show="step === 2" class="rounded-lg bg-slate-800 p-5">
    <h2 class="font-semibold mb-1">② 基础配置</h2>
    <p class="mb-4 text-xs text-slate-400">设置监听地址、端口与流量路由。保存后自动热重载，已有连接不受影响。</p>
    <div class="grid sm:grid-cols-2 gap-4 mb-4">
      <label class="block">
        <span class="text-xs text-slate-400">监听地址</span>
        <input x-model="form.listen" class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none" placeholder="127.0.0.1">
        <span class="text-xs text-slate-500">默认只监听本机；改 0.0.0.0 前请设置代理认证！</span>
      </label>
      <label class="block">
        <span class="text-xs text-slate-400">SOCKS5 端口</span>
        <input x-model.number="form.port" type="number" min="1" max="65535" class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none" placeholder="9051">
        <span class="text-xs text-slate-500">默认 9051</span>
      </label>
    </div>
    <div class="text-xs text-slate-400 mb-2">流量路由（三选一）</div>
    <div class="grid sm:grid-cols-3 gap-3 mb-4">
      <template x-for="r in (env ? env.route_options : [])" :key="r.name">
        <label class="cursor-pointer block">
          <input type="radio" name="wz-route" x-model="form.route" :value="r.name" class="peer sr-only">
          <div class="rounded border p-3 h-full transition"
            :class="form.route === r.name ? 'border-indigo-500 bg-indigo-900/40' : 'border-slate-600 bg-slate-700/40 hover:border-slate-500'">
            <div class="text-sm font-semibold" x-text="r.name"></div>
            <div class="text-xs text-slate-300" x-text="r.title"></div>
            <div class="text-xs text-slate-400 mt-1" x-text="r.summary"></div>
          </div>
        </label>
      </template>
    </div>
    <div class="grid sm:grid-cols-2 gap-4 mb-4">
      <label class="block">
        <span class="text-xs text-slate-400">代理认证用户名（选填）</span>
        <input x-model="form.username" autocomplete="off" class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none" placeholder="留空 = 不需要认证">
      </label>
      <label class="block">
        <span class="text-xs text-slate-400">代理认证密码（选填）</span>
        <input x-model="form.password" type="password" autocomplete="new-password" class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none" placeholder="留空 = 保持不变">
      </label>
    </div>
    <div class="rounded px-3 py-2 text-sm" :class="saveOk ? 'bg-emerald-900/40 text-emerald-300' : 'bg-rose-900/40 text-rose-300'"
      x-show="saveMsg" x-text="saveMsg"></div>
    <div class="mt-4 flex justify-between">
      <button @click="prev()" class="rounded border border-slate-600 hover:bg-slate-700 px-4 py-2 text-sm">上一步</button>
      <button @click="saveConfig()" :disabled="saving" class="rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-4 py-2 text-sm font-medium"
        x-text="saving ? '保存中…' : '保存并应用'"></button>
    </div>
    <div class="mt-3 text-right">
      <button @click="next()" class="text-xs text-slate-400 hover:text-white">跳过，下一步 →</button>
    </div>
  </section>

  <!-- 第 3 步：出口与网桥 -->
  <section x-show="step === 3" class="rounded-lg bg-slate-800 p-5">
    <h2 class="font-semibold mb-1">③ 出口与网桥</h2>

    <!-- 中继类路由：填写中继地址 -->
    <template x-if="needsRelay">
      <div>
        <p class="mb-3 text-xs text-slate-400" x-text="form.route === 'self-relay'
          ? '自建中继（TSU/1 协议）：在一台可被访问的机器上运行 `torsocks5 relay serve --port 9052 --token <令牌>`，然后把地址填到下面；本机测试可填 ws://127.0.0.1:9052/tsu。多节点与负载均衡配置见 docs/routes.md。'
          : 'Cloudflare Worker 中转：先自行部署 Worker（v2.0 起部署文件移出本仓库），把生成的地址与令牌填到下面。详见 docs/routes.md。'"></p>
        <div class="grid sm:grid-cols-2 gap-4">
          <label class="block">
            <span class="text-xs text-slate-400" x-text="(relaySection === 'cf_relay' ? 'Worker' : '中继') + ' 地址'"></span>
            <input x-model="form.relay_url" placeholder="wss://relay.example.com/tsu" class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm font-mono focus:border-indigo-500 focus:outline-none">
          </label>
          <label class="block">
            <span class="text-xs text-slate-400">令牌（选填）</span>
            <input x-model="form.relay_token" type="password" autocomplete="new-password" placeholder="留空 = 保持不变"
              class="mt-1 w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm font-mono focus:border-indigo-500 focus:outline-none">
          </label>
        </div>
        <div class="mt-3 rounded bg-slate-700/50 px-3 py-2 text-xs text-slate-400">
          <span x-text="'当前已配置：' + (env && env.relay.url ? env.relay.url : '（未配置）')"></span>
          <span x-show="env && env.relay.token_set"> · 令牌已设置</span>
          <button @click="saveConfig()" :disabled="saving" class="ml-3 rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-3 py-1 text-xs font-medium"
            x-text="saving ? '保存中…' : '保存中继配置'"></button>
        </div>
      </div>
    </template>

    <!-- tor-meek：网桥工具 -->
    <template x-if="!needsRelay">
      <div>
        <p class="mb-3 text-xs text-slate-400">tor-meek 路由通过 meek 网桥连接 Tor。国内网络建议至少保存 1-2 条网桥备用。</p>
        <div class="rounded bg-slate-700/50 px-3 py-2 text-sm mb-3 flex items-center justify-between">
          <span>已启用网桥：<b x-text="bridgeCount"></b> 条</span>
          <button @click="fetchBridges()" :disabled="fetching" class="rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-3 py-1.5 text-xs font-medium"
            x-text="fetching ? '请求中…（最多 30 秒）' : '一键向 tor 官网请求'"></button>
        </div>
        <div class="rounded px-3 py-2 text-sm mb-3" :class="fetchOk ? 'bg-emerald-900/40 text-emerald-300' : 'bg-amber-900/40 text-amber-300'"
          x-show="fetchMsg" x-text="fetchMsg"></div>
        <details class="mb-3 rounded bg-slate-700/40 px-3 py-2 text-xs text-slate-300">
          <summary class="cursor-pointer font-medium">自动获取失败？用邮件 / 网页方式</summary>
          <ol class="mt-2 list-decimal ml-5 space-y-1 text-slate-400">
            <li>发邮件到 <code class="text-slate-200">bridges@torproject.org</code>，正文只写一行：<code class="text-slate-200">get transport meek</code></li>
            <li>或打开 <a href="https://bridges.torproject.org" target="_blank" rel="noopener" class="underline text-indigo-300">bridges.torproject.org</a> 选择 Meek 类型复制网桥</li>
            <li>把收到的 Bridge 行粘贴到下方输入框</li>
          </ol>
        </details>
        <div class="flex gap-2">
          <input x-model="manualLine" @keyup.enter="addManual()" placeholder="Bridge meek 0.0.2.0:3 url=... front=..."
            class="flex-1 rounded bg-slate-900 border border-slate-600 px-3 py-2 text-sm font-mono focus:border-indigo-500 focus:outline-none">
          <button @click="addManual()" class="rounded bg-slate-700 hover:bg-slate-600 px-3 py-2 text-sm">添加</button>
        </div>
        <div class="mt-2 rounded px-3 py-2 text-sm" :class="manualOk ? 'bg-emerald-900/40 text-emerald-300' : 'bg-rose-900/40 text-rose-300'"
          x-show="manualMsg" x-text="manualMsg"></div>
      </div>
    </template>

    <div class="mt-4 flex justify-between">
      <button @click="prev()" class="rounded border border-slate-600 hover:bg-slate-700 px-4 py-2 text-sm">上一步</button>
      <button @click="next()" class="rounded bg-indigo-600 hover:bg-indigo-500 px-4 py-2 text-sm font-medium">下一步：开机自启</button>
    </div>
  </section>

  <!-- 第 4 步：开机自启 -->
  <section x-show="step === 4" class="rounded-lg bg-slate-800 p-5">
    <h2 class="font-semibold mb-1">④ 开机自启（可选）</h2>
    <p class="mb-3 text-xs text-slate-400" x-text="service ? ('已为当前系统生成 ' + service.title + ' 配置。') : ''"></p>
    <div class="text-sm text-slate-400" x-show="serviceLoading">正在生成…</div>
    <template x-if="service && !serviceLoading">
      <div>
        <textarea readonly x-model="service.content" rows="10"
          class="w-full rounded bg-slate-900 border border-slate-600 px-3 py-2 text-xs font-mono focus:outline-none"></textarea>
        <div class="mt-2 flex gap-2 items-center">
          <button @click="copyService()" class="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs">复制内容</button>
          <button @click="downloadService()" class="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs">下载文件</button>
          <button @click="loadService()" class="text-xs text-slate-400 hover:text-white">重新生成</button>
          <span class="text-xs text-emerald-400" x-text="copied"></span>
        </div>
        <ol class="mt-3 list-decimal ml-5 space-y-1 text-sm text-slate-300">
          <template x-for="(s, i) in service.steps" :key="i"><li x-text="s"></li></template>
        </ol>
        <div class="mt-3 text-xs text-slate-500">
          卸载：<code class="font-mono text-slate-400" x-text="service.uninstall"></code>
        </div>
      </div>
    </template>
    <div class="mt-4 flex justify-between">
      <button @click="prev()" class="rounded border border-slate-600 hover:bg-slate-700 px-4 py-2 text-sm">上一步</button>
      <button @click="next()" class="rounded bg-indigo-600 hover:bg-indigo-500 px-4 py-2 text-sm font-medium">下一步：完成</button>
    </div>
  </section>

  <!-- 第 5 步：完成 -->
  <section x-show="step === 5" class="rounded-lg bg-slate-800 p-5">
    <h2 class="font-semibold mb-3">⑤ 完成</h2>
    <div class="grid sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-4" x-show="status">
      <div class="rounded bg-slate-700/50 p-3">
        <div class="text-xs text-slate-400">SOCKS5 地址</div>
        <div class="text-sm font-semibold font-mono break-all" x-text="status ? status.proxy.listen : '-'"></div>
      </div>
      <div class="rounded bg-slate-700/50 p-3">
        <div class="text-xs text-slate-400">当前路由</div>
        <div class="text-sm font-semibold" x-text="status ? status.route.name : '-'"></div>
      </div>
      <div class="rounded bg-slate-700/50 p-3">
        <div class="text-xs text-slate-400">已启用网桥</div>
        <div class="text-sm font-semibold" x-text="bridgeCount + ' 条'"></div>
      </div>
      <div class="rounded bg-slate-700/50 p-3">
        <div class="text-xs text-slate-400">遗留问题</div>
        <div class="text-sm font-semibold" :class="failedChecks ? 'text-amber-400' : 'text-emerald-400'"
          x-text="failedChecks ? (failedChecks + ' 项待处理') : '无'"></div>
      </div>
    </div>
    <div class="rounded bg-slate-700/40 px-3 py-2 text-sm text-slate-300 mb-4">
      浏览器/系统代理设置为 <b>SOCKS5 127.0.0.1 端口见上方</b>；
      远程 DNS 请使用 <code class="text-xs">socks5h://</code> 前缀以避免本地 DNS 泄漏。
    </div>
    <div class="flex justify-between">
      <button @click="prev()" class="rounded border border-slate-600 hover:bg-slate-700 px-4 py-2 text-sm">上一步</button>
      <div class="flex gap-2">
        <button @click="go(1); loadEnv()" class="rounded border border-slate-600 hover:bg-slate-700 px-4 py-2 text-sm">重新检测</button>
        <a href="/" class="rounded bg-indigo-600 hover:bg-indigo-500 px-4 py-2 text-sm font-medium">打开仪表盘 →</a>
      </div>
    </div>
  </section>
</div>
<script>
function wizard() {
  return {
    steps: ['环境检测', '基础配置', '出口与网桥', '开机自启', '完成'],
    step: 1,
    env: null, envLoading: false, envError: '',
    form: {listen: '127.0.0.1', port: 9051, route: 'tor-meek',
           username: '', password: '', relay_url: '', relay_token: ''},
    saving: false, saveMsg: '', saveOk: true,
    fetching: false, fetchMsg: '', fetchOk: true,
    manualLine: '', manualMsg: '', manualOk: true,
    service: null, serviceLoading: false, copied: '',
    status: null, bridgeCount: 0,

    async init() { await this.loadEnv(); },

    get needsRelay() {
      return this.form.route === 'cf-relay' || this.form.route === 'self-relay';
    },
    get relaySection() {
      return this.form.route === 'cf-relay' ? 'cf_relay' : 'self_relay';
    },
    get failedChecks() {
      return this.env ? this.env.checks.filter(c => !c.ok).length : 0;
    },

    async loadEnv() {
      this.envLoading = true; this.envError = '';
      try {
        const r = await fetch('/api/wizard/env');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const j = await r.json();
        this.env = j;
        this.bridgeCount = j.bridge_count;
        this.form.listen = j.form.listen;
        this.form.port = j.form.port;
        this.form.route = j.form.route;
        this.form.username = j.form.username || '';
        this.form.relay_url = j.relay.url || '';
        this.form.password = '';
      } catch (e) { this.envError = '检测失败：' + e; }
      this.envLoading = false;
    },
    async go(n) {
      this.step = n;
      if (n === 4 && !this.service) this.loadService();
      if (n === 5) this.loadStatus();
    },
    next() { this.go(Math.min(5, this.step + 1)); },
    prev() { this.go(Math.max(1, this.step - 1)); },

    async saveConfig() {
      this.saving = true; this.saveMsg = '';
      const body = {proxy: {listen: this.form.listen,
                            port: Number(this.form.port),
                            route: this.form.route}};
      if (this.form.username) body.proxy.username = this.form.username;
      if (this.form.password) body.proxy.password = this.form.password;
      if (this.needsRelay && this.form.relay_url) {
        body[this.relaySection] = {url: this.form.relay_url};
        if (this.form.relay_token) body[this.relaySection].token = this.form.relay_token;
      }
      try {
        const r = await fetch('/api/wizard/config', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)});
        const j = await r.json();
        this.saveOk = !!j.success;
        if (j.success) {
          let msg = '已保存' + (j.reloaded ? '并热重载（' + (j.changes || []).length + ' 项变更）' : '');
          if (j.restart_required) msg += '；监听/端口变更需重启进程生效';
          if (j.warnings && j.warnings.length) msg += '；' + j.warnings.join('；');
          if (j.error) msg += '；' + j.error;
          this.saveMsg = msg;
        } else {
          this.saveMsg = '失败：' + (j.error || '未知错误');
        }
      } catch (e) { this.saveOk = false; this.saveMsg = '请求失败：' + e; }
      this.saving = false;
    },

    async fetchBridges() {
      this.fetching = true; this.fetchMsg = ''; this.fetchOk = true;
      try {
        const r = await fetch('/api/wizard/fetch', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({transport: 'meek'})});
        const j = await r.json();
        this.fetchOk = !!j.success;
        if (j.success) {
          this.fetchMsg = '成功：获取 ' + j.found + ' 条，新增 ' + j.added +
            ' 条（已存在的会重新启用）';
          this.bridgeCount = j.count;
        } else {
          this.fetchMsg = 'HTTPS 获取失败' + (j.error ? '（' + j.error + '）' : '') +
            '。国内网络访问可能受限：可用邮件/网页方式获取，或直接粘贴网桥行。';
        }
      } catch (e) { this.fetchOk = false; this.fetchMsg = '请求失败：' + e; }
      this.fetching = false;
    },
    async addManual() {
      if (!this.manualLine.trim()) return;
      this.manualMsg = '';
      try {
        const r = await fetch('/api/bridges', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: 'add', line: this.manualLine.trim()})});
        const j = await r.json();
        this.manualOk = !!j.success;
        this.manualMsg = j.success ? '已添加并保存' : ('失败：' + (j.error || ''));
        if (j.success) { this.manualLine = ''; await this.refreshBridges(); }
      } catch (e) { this.manualOk = false; this.manualMsg = '请求失败：' + e; }
    },
    async refreshBridges() {
      try {
        const r = await fetch('/api/bridges');
        const j = await r.json();
        if (j.active != null) this.bridgeCount = j.active;
      } catch (e) { /* 忽略刷新失败 */ }
    },

    async loadService() {
      this.serviceLoading = true;
      try {
        const r = await fetch('/api/wizard/service');
        this.service = await r.json();
      } catch (e) { this.service = null; }
      this.serviceLoading = false;
    },
    async copyService() {
      try {
        await navigator.clipboard.writeText(this.service.content);
        this.copied = '已复制';
      } catch (e) { this.copied = '复制失败，请手动全选文本'; }
      setTimeout(() => { this.copied = ''; }, 2000);
    },
    downloadService() {
      const blob = new Blob([this.service.content],
                            {type: 'text/plain;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = this.service.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    },
    async loadStatus() {
      try {
        const r = await fetch('/api/status');
        this.status = await r.json();
      } catch (e) { this.status = null; }
    },
  };
}
</script>
"""
    return _shell("配置向导", "/wizard", body)


PAGES: Dict[str, "Callable[[], str]"] = {
    "/": dashboard_page,
    "/routes": routes_page,
    "/bridges": bridges_page,
    "/config": config_page,
    "/logs": logs_page,
    "/wizard": wizard_page,
}


__all__ = ["PAGES", "NAV", "dashboard_page", "routes_page", "bridges_page",
           "config_page", "logs_page"]
