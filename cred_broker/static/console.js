"use strict";

const byId = (id) => document.getElementById(id);
const content = byId("content");
const dialog = byId("dialog");
const dialogContent = byId("dialog-content");
const dialogError = byId("dialog-error");
const pages = {
  catalog: ["连接目录", "选择服务并创建连接。"],
  connections: ["我的连接", "查看已配置的资源，测试连接或删除连接。"],
  grants: ["AI 授权", "指定 AI 可以访问的连接、资源、工具和有效期。"],
  audit: ["操作记录", "查看网关记录的操作及结果。"],
};
const catalog = [
  ["gmail", "Gmail", "✉"],
  ["ssh", "SSH", ">_"],
  ["github", "GitHub", "⌘"],
  ["instabox", "Instabox", "⬡"],
  ["feishu", "飞书", "◇"],
  ["instagram", "Instagram", "◎"],
];
let revision = 0;
let dialogGeneration = 0;
let pageController;
let noticeTimer;
const health = new Map();
const filters = {catalog: {query: "", category: "全部"}, connections: "", grantQuery: "", grants: "全部", audit: {query: "", outcome: "全部"}};
const providerInfo = {
  gmail: {category: "邮箱", description: "在明确授权的邮箱中搜索与阅读邮件，让 AI 帮你整理信息。", capabilities: ["搜索邮件", "阅读邮件"], auth: "Google OAuth · 只读"},
  github: {category: "代码托管", description: "连接 GitHub App，按仓库授予 AI 代码与项目的只读访问。", capabilities: ["查看仓库", "读取文件", "查询议题"], auth: "GitHub App · 只读"},
  ssh: {category: "远程主机", description: "通过固定主机密钥连接服务器，查询指定服务的状态与日志。", capabilities: ["服务状态", "服务日志"], auth: "SSH 密钥 · 固定主机校验"},
  instabox: {category: "远程主机", description: "查看指定云机器的状态，可按需开启受限的主机诊断。", capabilities: ["机器状态", "受限诊断"], auth: "API 密钥 · 机器白名单"},
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("ui-icon"); svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("width", "18"); svg.setAttribute("height", "18");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "/static/ui-icons.svg#" + name); svg.append(use); return svg;
}
function providerImage(id) {
  const img = node("img", undefined, "provider-icon"); img.src = "/static/icons/" + id + ".svg";
  img.alt = ""; img.width = 40; img.height = 40; return img;
}
function tags(values, className = "capability-tags") {
  const group = node("div", undefined, className);
  for (const value of values) group.append(node("span", value, "permission-chip"));
  return group;
}
function chips(values, selected, onChange, label) {
  const group = node("div", undefined, "filter-chips"); group.setAttribute("role", "group"); group.setAttribute("aria-label", label);
  for (const value of values) {
    const item = button(value, () => onChange(value), "filter-chip" + (selected === value ? " is-active" : ""));
    item.setAttribute("aria-pressed", String(selected === value)); group.append(item);
  } return group;
}
function searchField(label, placeholder, value, callback) {
  const field = node("div", undefined, "search-field"); field.append(icon("search"));
  const input = node("input", undefined, "search-input"); input.type = "search"; input.placeholder = placeholder;
  input.setAttribute("aria-label", label); input.value = value; input.addEventListener("input", () => callback(input.value));
  field.append(input); return field;
}
function setPageAction(text, callback, disabled = false) {
  const action = byId("page-action"); action.replaceChildren(icon("plus"), document.createTextNode(text));
  action.hidden = false; action.disabled = disabled; action.onclick = callback;
}
async function navigate(page) {
  if (location.hash === "#" + page) await render();
  else location.hash = page;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function button(text, callback, className) {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", callback);
  return element;
}

async function api(path, method = "GET", body, signal) {
  const response = await fetch("/admin/api/v1" + path, {
    method, credentials: "same-origin", cache: "no-store", signal,
    ...(body === undefined ? {} : {
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    }),
  });
  if (response.status === 401) {
    location.assign("/");
    throw new Error("会话已过期，请重新登录。");
  }
  if (!response.ok) {
    const messages = {400: "请检查输入后重试。", 403: "当前操作无权限。", 404: "资源不存在或已删除。", 409: "操作与现有记录冲突。", 422: "输入未通过校验，请检查字段。"};
    throw new Error(messages[response.status] || "操作失败，请稍后重试。");
  }
  return response.status === 204 ? {} : response.json();
}

function notice(text) {
  const element = byId("notice");
  clearTimeout(noticeTimer);
  element.textContent = text;
  element.hidden = !text;
  if (text) noticeTimer = setTimeout(() => {element.hidden = true;}, 6000);
}

function fail(error) {
  dialogError.textContent = error instanceof Error ? error.message : "操作失败，请重试。";
  dialogError.hidden = false; dialogError.tabIndex = -1; dialogError.focus();
}

function showDialog(title) {
  dialogGeneration++;
  dialogContent.replaceChildren();
  dialogError.hidden = true;
  byId("dialog-title").textContent = title;
  if (!dialog.open) dialog.showModal();
}

async function showDiagnostics() {
  showDialog("运行诊断");
  const generation = dialogGeneration;
  dialogContent.append(node("p", "正在检查运行状态…", "loading-state"));
  try {
    const ready = await api("/ready", "GET");
    if (generation !== dialogGeneration || !dialog.open) return;
    dialogContent.replaceChildren();
    const table = node("dl", undefined, "diagnostic-list");
    for (const [label, key] of [["版本", "version"], ["存储", "storage"], ["主密钥", "key"], ["迁移", "migrations"]]) {
      table.append(node("dt", label), node("dd", ready[key] ?? "未知"));
    }
    table.append(node("dt", "支持的服务"), node("dd", Array.isArray(ready.providers) ? ready.providers.join("、") || "暂无" : "未知"));
    dialogContent.append(table);
  } catch (error) {
    if (generation !== dialogGeneration || !dialog.open) return;
    dialogContent.replaceChildren(node("p", error instanceof Error ? error.message : "诊断失败，请重试。", "empty-state"));
  }
}

function closeDialog() {
  dialogGeneration++; dialogContent.replaceChildren(); dialog.close();
}

dialog.addEventListener("cancel", (event) => { if (dialog.dataset.preventCancel === "true") { event.preventDefault(); return; } dialogGeneration++; dialogContent.replaceChildren(); });
byId("close-dialog").addEventListener("click", closeDialog);
byId("diagnostics")?.addEventListener("click", showDiagnostics);
dialog.addEventListener("close", () => { dialogGeneration++; dialogContent.replaceChildren(); });

function submitForm(form, text, action, options = {}) {
  let submitting = false;
  const submit = node("button", text);
  submit.type = "submit";
  const actions = node("div", undefined, "dialog-actions");
  const cancel = button("取消", closeDialog, "quiet"); actions.append(cancel, submit); form.append(actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    submitting = true;
    const generation = dialogGeneration;
    submit.disabled = true;
    submit.textContent = "正在处理…";
    if (options.preventClose) { dialog.dataset.preventCancel = "true"; cancel.disabled = true; byId("close-dialog").disabled = true; }
    dialogError.hidden = true;
    try { await action(new FormData(form), () => generation === dialogGeneration && dialog.open); }
    catch (error) { if (generation === dialogGeneration) fail(error); }
    finally { submitting = false; submit.disabled = false; submit.textContent = text; if (options.preventClose) { delete dialog.dataset.preventCancel; cancel.disabled = false; byId("close-dialog").disabled = false; } }
  });
}

function field(form, name, label, options = {}) {
  const wrapper = node("label", label);
  const input = node(options.multiline ? "textarea" : "input");
  if (!options.multiline) input.type = options.type || "text";
  input.name = name;
  input.setAttribute("aria-label", label);
  input.required = options.required !== false;
  input.autocomplete = "off";
  input.spellcheck = false;
  if (options.placeholder) input.placeholder = options.placeholder;
  if (options.value !== undefined) input.value = options.value;
  if (options.min !== undefined) input.min = options.min;
  if (options.max !== undefined) input.max = options.max;
  wrapper.append(input);
  form.append(wrapper);
  if (options.help) { const help = node("small", options.help, "field-help"); help.id = "help-" + name; input.setAttribute("aria-describedby", help.id); form.append(help); }
  return input;
}

function createConnection(provider, oauthEnabled) {
  showDialog("连接 " + provider.name);
  if (provider.id === "gmail" && !oauthEnabled) {
    dialogContent.append(node("p", "请管理员配置 CB_GOOGLE_CLIENT_ID、CB_GOOGLE_CLIENT_SECRET 和 CB_GOOGLE_REDIRECT_URI。"));
    return;
  }
  const form = node("form");
  const context = node("div", undefined, "form-context");
  context.append(providerImage(provider.id), node("p", providerInfo[provider.id]?.description || provider.name)); form.append(context);
  field(form, "label", "连接名称", {placeholder: "例如：团队工作空间", help: "使用易辨认的名称，签发授权时会显示此名称。"});
  let config = () => ({});
  const list = (value) => String(value || "").split(/[\n,]+/).map((part) => part.trim()).filter(Boolean);
  if (provider.id === "ssh") {
    field(form, "host", "主机");
    field(form, "port", "端口", {type: "number", min: 1, max: 65535, value: 22});
    field(form, "user", "用户名");
    field(form, "host_key", "固定的 SSH 主机公钥");
    field(form, "services", "允许的服务（每行一个）", {multiline: true, placeholder: "example.service"});
    field(form, "private_key", "SSH 私钥", {multiline: true});
    config = (data) => ({
      settings: {host: data.get("host").trim(), port: Number(data.get("port")), user: data.get("user").trim(), host_key: data.get("host_key").trim(), services: list(data.get("services"))},
      secret: {private_key: data.get("private_key").trim()},
    });
  } else if (provider.id === "github") {
    field(form, "app_id", "GitHub App ID");
    field(form, "installation_id", "安装 ID");
    field(form, "repositories", "允许访问的仓库（每行一个）", {multiline: true, placeholder: "owner/repository"});
    field(form, "private_key", "GitHub App 私钥", {multiline: true});
    config = (data) => ({
      settings: {app_id: data.get("app_id").trim(), installation_id: data.get("installation_id").trim(), repositories: list(data.get("repositories"))},
      secret: {private_key: data.get("private_key").trim()},
    });
  } else if (provider.id === "instabox") {
    field(form, "machines", "允许访问的机器 ID（每行一个）", {multiline: true});
    field(form, "read_key", "读取密钥", {type: "password"});
    field(form, "diagnostic_key", "诊断密钥（可选，machines:write）", {type: "password", required: false});
    form.append(node("p", "仅配置诊断密钥后才提供诊断工具；网关仅允许运行时长、磁盘和内存检查。"));
    config = (data) => ({
      settings: {machines: list(data.get("machines"))},
      secret: {read_key: data.get("read_key").trim(), ...(data.get("diagnostic_key").trim() ? {diagnostic_key: data.get("diagnostic_key").trim()} : {})},
    });
  }
  submitForm(form, provider.id === "gmail" ? "前往 Google 授权" : "创建连接", async (data, isCurrent) => {
    const label = data.get("label").trim();
    if (!label) throw new Error("请输入连接名称。");
    if (provider.id === "gmail") {
      location.assign("/admin/oauth/google/start?" + new URLSearchParams({label}));
      return;
    }
    const configured = config(data);
    for (const value of Object.values(configured.settings)) {
      if (value === "" || (Array.isArray(value) && !value.length)) throw new Error("请填写连接设置。");
    }
    try {
      await api("/connections", "POST", {provider: provider.id, label, ...configured});
    } finally {
      for (const key of Object.keys(configured.secret)) {
        data.delete(key);
        form.elements.namedItem(key).value = "";
      }
    }
    if (isCurrent()) {dialog.close(); await navigate("connections");}
    else await render();
    notice("连接已创建。");
  });
  dialogContent.append(form);
}

function card(title, text) {
  const element = node("article", undefined, "card");
  element.append(node("h2", title));
  if (text) element.append(node("p", text));
  return element;
}

function empty(text, target = content, action) {
  const element = node("div", undefined, "empty-state");
  const mark = node("div", undefined, "empty-state-icon"); mark.append(icon("search"));
  element.append(mark, node("h2", text));
  if (action) element.append(button("浏览连接目录", () => navigate("catalog"), "quiet"));
  target.append(element);
}

function renderCatalog({providers, google_oauth_enabled: oauthEnabled}, connections) {
  const toolbar = node("div", undefined, "catalog-toolbar");
  const results = node("div", undefined, "catalog-results");
  const draw = () => {
    results.replaceChildren();
    const chosen = catalog.filter(([id, name]) => {
      const info = providerInfo[id];
      return info && (filters.catalog.category === "全部" || info.category === filters.catalog.category) &&
        [name, info.category, info.description, ...info.capabilities].join(" ").toLowerCase().includes(filters.catalog.query.toLowerCase().trim());
    });
    const count = node("p", chosen.length + " 个可连接服务", "result-count"); count.setAttribute("role", "status"); results.append(count);
    const grid = node("div", undefined, "provider-grid");
    for (const [id, name] of chosen) {
      const provider = providers.find(item => item.id === id);
      const info = providerInfo[id];
      const item = node("article", undefined, "card provider-card");
      item.dataset.provider = id;
      const head = node("div", undefined, "provider-head"); const identity = node("div", undefined, "provider-identity");
      identity.append(node("h2", name), node("span", info.category, "provider-category"));
      head.append(providerImage(id), identity);
      item.append(head, node("p", info.description, "provider-description"), tags(info.capabilities));
      const footer = node("div", undefined, "provider-footer");
      const method = node("span", undefined, "auth-method"); method.append(icon("lock"), document.createTextNode(info.auth));
      const actions = node("div", undefined, "actions");
      const connect = button("连接", () => createConnection({...provider, id, name}, oauthEnabled)); connect.disabled = !provider?.supported;
      actions.append(connect);
      const existing = connections.filter(connection => connection.provider === id);
      if (existing.length) actions.append(button(existing.length === 1 ? "测试" : "选择连接并测试", () => {
        if (existing.length === 1) return testConnection(existing[0]);
        showDialog("测试 " + name); const choices = node("div", undefined, "list");
        for (const connection of existing) choices.append(button(connection.label, () => testConnection(connection), "quiet"));
        dialogContent.append(choices);
      }, "quiet"));
      footer.append(method, actions); item.append(footer); grid.append(item);
    }
    if (!chosen.length) empty("没有匹配的服务，试试其他关键词。", grid);
    results.append(grid);
  };
  toolbar.append(searchField("搜索服务", "搜索服务或用途", filters.catalog.query, value => {filters.catalog.query = value; draw();}));
  const chipContainer = node("div");
  const drawChips = () => chipContainer.replaceChildren(chips(["全部", "邮箱", "代码托管", "远程主机"], filters.catalog.category, value => {filters.catalog.category = value; drawChips(); draw();}, "服务类别"));
  drawChips(); toolbar.append(chipContainer);
  content.append(toolbar, results); draw();
  const future = node("section", undefined, "future-section"); future.append(node("h2", "更多服务，陆续接入"));
  const futureGrid = node("div", undefined, "future-grid");
  for (const [id, name] of catalog.filter(([id]) => !providerInfo[id])) {
    const item = node("article", undefined, "future-card"); item.append(providerImage(id), node("h2", name), node("span", "暂不支持", "badge")); futureGrid.append(item);
  }
  future.append(futureGrid); content.append(future);
  const note = node("aside", undefined, "permission-note"); note.append(icon("shield"), node("p", "连接只是第一步。AI 只能使用你随后明确授权的资源与工具，原始服务凭证不会提供给 AI。")); content.append(note);
}

async function testConnection(connection) {
  if (health.get(connection.id)?.testing) return;
  showDialog("测试 " + connection.label);
  const generation = dialogGeneration;
  health.set(connection.id, {testing: true});
  dialogContent.append(node("p", "正在测试连接…", "loading-state"));
  try {
    const {ok} = await api("/connections/" + encodeURIComponent(connection.id) + "/test", "POST");
    health.set(connection.id, {ok, checkedAt: Date.now()});
    if (generation === dialogGeneration) dialogContent.replaceChildren(node("p", ok ? "连接测试通过。" : "连接测试未通过，请检查服务配置和凭证。"));
    void render();
  } catch (error) {
    if (generation === dialogGeneration) {dialogContent.replaceChildren(); fail(error);}
  } finally {
    if (health.get(connection.id)?.testing) health.delete(connection.id);
  }
}

function confirmAction(title, description, action, success) {
  showDialog(title);
  const form = node("form");
  form.append(node("p", description, "form-context"));
  submitForm(form, "确认", async (_data, isCurrent) => {
    await action();
    if (isCurrent()) dialog.close();
    await render();
    notice(success);
  });
  dialogContent.append(form);
}

function renderConnections(connections) {
  setPageAction("添加连接", () => navigate("catalog"));
  if (!connections.length) return empty("还没有连接，先选择一个服务。", content, true);
  const list = node("div", undefined, "connection-list");
  const draw = () => {
  list.replaceChildren();
  const visible = connections.filter(connection => [connection.label, connection.provider, ...connection.resources.map(resource => resource.label)].join(" ").toLowerCase().includes(filters.connections.toLowerCase().trim()));
  for (const connection of visible) {
    const info = providerInfo[connection.provider]; const name = catalog.find(([id]) => id === connection.provider)?.[1] || connection.provider;
    const item = node("article", undefined, "card connection-row");
    const main = node("div", undefined, "connection-main"); main.append(node("h2", connection.label));
    const meta = node("div", undefined, "connection-meta"); meta.append(node("span", name), node("span", "创建于 " + date(connection.created_at)));
    const localHealth = health.get(connection.id);
    const checkedAt = localHealth?.checkedAt != null ? Math.floor(localHealth.checkedAt / 1000) : connection.health?.checked_at;
    const ok = localHealth?.ok ?? connection.health?.ok;
    const statusText = checkedAt != null ? (ok ? "测试通过" : "测试失败") : "已连接（未测试）";
    const status = node("span", statusText, "status-badge " + (checkedAt != null ? (ok ? "status-healthy" : "status-error") : "status-configured"));
    const tested = checkedAt != null ? node("span", "最近测试 " + date(checkedAt), "health-time") : node("span", "尚未测试", "health-time");
    main.append(meta, tested, tags(connection.resources.map(entry => entry.label), "connection-resources"));
    const actions = node("div", undefined, "actions connection-actions");
    actions.append(button("测试连接", () => testConnection(connection), "quiet"),
      button("删除连接", () => confirmAction("删除连接", "确定删除「" + connection.label + "」？关联授权将无法再接纳新的调用，已接纳的操作可能继续完成。", () => api("/connections/" + encodeURIComponent(connection.id), "DELETE"), "连接已删除。"), "danger"));
    item.append(providerImage(connection.provider), main, status, actions); list.append(item);
  }
  if (!visible.length) empty("没有匹配的连接。", list);
  };
  content.append(searchField("搜索连接", "搜索连接名称或资源", filters.connections, value => {filters.connections = value; draw();}), node("p", connections.length + " 个已配置连接", "result-count"), list);
  draw();
}

function showToken(token) {
  showDialog("网关令牌已签发");
  dialogContent.append(node("p", "令牌仅在此窗口显示一次。请复制并保管，关闭后无法再次查看。配置文件中的令牌可能以明文保存，请优先使用客户端的安全 secret 字段。"));
  const label = node("label", "网关令牌");
  const input = node("textarea", undefined, "secret-value token-input");
  input.readOnly = true; input.autocomplete = "off"; input.spellcheck = false; input.value = token; label.append(input);
  input.rows = 2;
  const status = node("p"); status.setAttribute("role", "status");
  const copy = async (value, feedback = "已复制。", fallback) => { try { await navigator.clipboard.writeText(value); status.textContent = feedback; } catch { fallback?.focus(); fallback?.select(); status.textContent = "无法访问剪贴板，内容已选中，请手动复制。"; } };
  dialogContent.append(label, button("复制令牌", () => copy(input.value, "令牌已复制。", input)), status);
  const instructions = node("div", undefined, "mcp-instructions");
  instructions.append(node("h3", "连接到 AI 客户端"), node("p", "以下示例使用通用 Streamable HTTP MCP。客户端需支持 HTTP transport 和自定义 Bearer header；请把令牌放入客户端安全 secret 字段或临时环境变量。"));
  const gateway = location.origin + "/mcp";
  function snippet(title, value, feedback) {
    const block = node("div", undefined, "config-snippet"); const heading = node("div", undefined, "config-snippet-head");
    const area = node("textarea", value, "config-snippet-value"); area.readOnly = true; area.rows = Math.min(8, value.split("\n").length);
    heading.append(node("strong", title), button("复制", () => copy(value, feedback, area), "quiet")); block.append(heading, area); return block;
  }
  instructions.append(
    snippet("通用 HTTP", "URL: " + gateway + "\nAuthorization: Bearer $CRED_BROKER_TOKEN", "通用 HTTP 配置已复制。"),
    snippet("通用客户端 JSON（需支持变量插值）", JSON.stringify({mcpServers: {"cred-broker": {type: "http", url: gateway, headers: {Authorization: "Bearer ${CRED_BROKER_TOKEN}"}}}}, null, 2), "JSON 配置已复制。"),
    snippet("curl smoke test", "read -rs -p '网关令牌: ' CRED_BROKER_TOKEN; export CRED_BROKER_TOKEN; echo\ncurl -sS " + gateway + " \\\n  -H \"Authorization: Bearer $CRED_BROKER_TOKEN\" \\\n  -H 'Accept: application/json, text/event-stream' \\\n  -H 'Content-Type: application/json' \\\n  --data '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"smoke\",\"version\":\"1\"}}}'\ncurl -sS " + gateway + " \\\n  -H \"Authorization: Bearer $CRED_BROKER_TOKEN\" \\\n  -H 'Accept: application/json, text/event-stream' \\\n  -H 'Content-Type: application/json' \\\n  --data '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}'", "curl 示例已复制。"),
  );
  dialogContent.append(instructions); input.focus();
}

function checkboxGroup(title, name, entries) {
  const group = node("fieldset");
  group.append(node("legend", title));
  for (const {value, label, details} of entries) {
    const line = node("label");
    const input = node("input");
    input.type = "checkbox";
    input.name = name;
    input.value = value;
    const text = node("span", label);
    if (details) text.append(node("small", details));
    line.append(input, text);
    group.append(line);
  }
  if (!entries.length) group.append(node("p", "没有可选项。"));
  return group;
}

function createGrant(connections) {
  showDialog("签发 AI 授权");
  const form = node("form");
  form.append(node("p", "仅选择完成当前任务所需的资源与工具，授权到期后令牌自动失效。", "form-context"));
  field(form, "client", "AI 客户端名称", {placeholder: "例如：项目维护助手"});
  const label = node("label", "连接");
  const select = node("select");
  select.setAttribute("aria-label", "连接");
  select.name = "connection";
  for (const connection of connections) {
    const option = node("option", connection.label);
    option.value = connection.id;
    select.append(option);
  }
  label.append(select);
  const scope = node("div", undefined, "list");
  function fillScope() {
    const connection = connections.find((item) => String(item.id) === select.value);
    scope.replaceChildren(
      checkboxGroup("资源", "resource", (connection?.resources || []).map((item) => ({value: item.id, label: item.label}))),
      checkboxGroup("工具", "tool", (connection?.tools || []).map((item) => ({value: item.name, label: item.name, details: item.description}))),
    );
  }
  select.addEventListener("change", fillScope);
  fillScope();
  form.append(label, scope);
  const ttlLabel = node("label", "有效期");
  const ttl = node("select");
  ttl.setAttribute("aria-label", "有效期");
  ttl.name = "ttl";
  for (const [seconds, text] of [[900, "15 分钟"], [3600, "1 小时"], [28800, "8 小时"], [86400, "24 小时"]]) {
    const option = node("option", text);
    option.value = seconds;
    ttl.append(option);
  }
  ttlLabel.append(ttl);
  form.append(ttlLabel);
  submitForm(form, "签发令牌", async (data, isCurrent) => {
    const client = data.get("client").trim();
    const resources = data.getAll("resource");
    const tools = data.getAll("tool");
    if (!client || !resources.length || !tools.length) throw new Error("请填写客户端名称，并至少选择一个资源和一个工具。");
    const {token} = await api("/grants", "POST", {client, connection_id: select.value, resources, tools, ttl_seconds: Number(data.get("ttl"))});
    if (isCurrent()) showToken(token);
    else notice("授权已签发，但令牌窗口已取消；请撤销该授权并重新签发。");
    void render();
  });
  dialogContent.append(form);
}

function date(seconds) {
  return seconds == null ? "—" : new Date(seconds * 1000).toLocaleString("zh-CN");
}

function grantState(grant) {
  if (grant.revoked_at != null) return "已撤销";
  const seconds = grant.expires_at - Date.now() / 1000;
  return seconds <= 0 ? "已过期" : seconds <= 3600 ? "即将过期" : "有效";
}

function remaining(seconds) {
  const total = Math.max(0, Math.round(seconds - Date.now() / 1000));
  if (!total) return "已到期";
  if (total < 3600) return "剩余 " + Math.max(1, Math.ceil(total / 60)) + " 分钟";
  if (total < 86400) return "剩余 " + Math.ceil(total / 3600) + " 小时";
  return "剩余 " + Math.ceil(total / 86400) + " 天";
}

function renewGrant(grant) {
  showDialog("重签发授权");
  const form = node("form"); form.append(node("p", "旧令牌会立即拒绝新的调用，已接纳的操作可能继续完成。新令牌只显示一次。", "form-context"));
  const ttl = node("select"); ttl.setAttribute("aria-label", "新的有效期"); ttl.name = "ttl";
  for (const [seconds, text] of [[900, "15 分钟"], [3600, "1 小时"], [28800, "8 小时"], [86400, "24 小时"]]) { const option = node("option", text); option.value = seconds; ttl.append(option); }
  const label = node("label", "新的有效期"); label.append(ttl); form.append(label);
  submitForm(form, "重签发并显示令牌", async (data, isCurrent) => {
    const result = await api("/grants/" + encodeURIComponent(grant.id) + "/reissue", "POST", {ttl_seconds: Number(data.get("ttl"))});
    if (isCurrent()) showToken(result.token); else notice("授权已重签发，但令牌窗口已取消。请重新重签发。");
    void render();
  }, {preventClose: true});
  dialogContent.append(form);
}

function renderGrants(grants, connections) {
  setPageAction("新建授权", () => createGrant(connections), !connections.length);
  if (!grants.length) return empty(connections.length ? "还没有 AI 授权，请按任务需要签发。" : "请先创建连接，再签发 AI 授权。", content, !connections.length);
  const controls = node("div", undefined, "catalog-toolbar"); const list = node("div", undefined, "grant-list");
  const draw = () => {
    list.replaceChildren();
    const selected = grants.filter(grant => {
      const state = grantState(grant);
      return (filters.grants === "全部" || state === filters.grants) && [grant.client, connections.find(connection => String(connection.id) === String(grant.connection_id))?.label, ...grant.resources, ...grant.tools].join(" ").toLowerCase().includes(filters.grantQuery.toLowerCase().trim());
    });
    for (const grant of selected) {
      const connection = connections.find(item => String(item.id) === String(grant.connection_id));
      const state = grantState(grant); const item = node("article", undefined, "card grant-card");
      const head = node("div", undefined, "grant-head"); const identity = node("div");
      identity.append(node("h2", grant.client), node("p", connection?.label || "连接已删除", "grant-meta"));
      const statusClass = state === "有效" ? "status-healthy" : state === "已撤销" ? "status-revoked" : "status-expired";
      head.append(identity, node("span", state, "status-badge " + statusClass));
      const scope = node("div", undefined, "grant-scope"); scope.append(node("span", "资源", "scope-label"), tags(grant.resources.map(id => connection?.resources.find(resource => resource.id === id)?.label || id)), node("span", "工具", "scope-label"), tags(grant.tools));
      const active = state === "有效" || state === "即将过期";
      const meta = node("p", "签发于 " + date(grant.created_at) + " · 到期时间：" + date(grant.expires_at) + " · " + (active ? remaining(grant.expires_at) : state), "grant-meta");
      item.append(head, scope, meta);
      if (state !== "已撤销") {
        const actions = node("div", undefined, "actions");
        actions.append(button("重签发", () => renewGrant(grant), "quiet"));
        if (active) actions.append(button("撤销授权", () => confirmAction("撤销授权", "撤销后该令牌将立即拒绝新的调用；已接纳的操作可能继续完成。", () => api("/grants/" + encodeURIComponent(grant.id) + "/revoke", "POST"), "授权已撤销。"), "danger"));
        item.append(actions);
      }
      list.append(item);
    }
    if (!selected.length) empty("当前状态下没有授权。", list);
    controls.replaceChildren(chips(["全部", "有效", "即将过期", "已过期", "已撤销"], filters.grants, value => {filters.grants = value; draw();}, "授权状态"));
  };
  content.append(searchField("搜索授权", "搜索客户端、资源或工具", filters.grantQuery, value => {filters.grantQuery = value; draw();}), controls, list); draw();
}
const actionNames = {
  "grant.reissue": "重签发授权", "gmail_search": "搜索邮件", "gmail_read": "读取邮件",
  "ssh_status": "查询服务状态", "ssh_logs": "读取服务日志", "github_contents": "读取仓库文件",
  "github_issues": "查询议题", "github_pulls": "查询合并请求", "instabox_describe": "查询机器状态",
  "instabox_diagnostics": "运行受限诊断", "unknown_tool": "未授权工具",
  "connection.create": "创建连接", "connection.created": "创建连接", "connection.test": "测试连接", "connection.delete": "删除连接", "connection.disconnect": "删除连接",
  "connection_create": "创建连接", "connection_test": "测试连接", "connection_delete": "删除连接", "grant_create": "签发授权", "grant_revoke": "撤销授权", "grant.create": "签发授权", "grant.revoke": "撤销授权", "gmail.search": "搜索邮件", "gmail.read": "阅读邮件",
  "ssh.status": "查询服务状态", "ssh.logs": "读取服务日志", "github.repositories": "查看仓库", "github.file": "读取仓库文件",
  "github.issues": "查询议题", "instabox.describe": "查询机器状态", "instabox.diagnostics": "运行受限诊断",
};
const outcomes = {ok: "成功", success: "成功", denied: "已拒绝", error: "失败", failed: "失败"};
function renderAudit(audit, connections) {
  const toolbar = node("div", undefined, "audit-toolbar");
  const holder = node("div"); const resultFilter = node("div");
  const dateRange = node("div", undefined, "audit-date-range");
  const from = node("input"); from.type = "date"; from.id = "audit-from";
  const to = node("input"); to.type = "date"; to.id = "audit-to";
  const fromLabel = node("label", "开始日期"); fromLabel.htmlFor = from.id;
  const toLabel = node("label", "结束日期"); toLabel.htmlFor = to.id;
  const feedback = node("p", "按浏览器本地日期导出，最多 31 天；留空导出最近 31 天。", "field-help");
  feedback.id = "audit-export-feedback"; feedback.setAttribute("role", "status");
  from.setAttribute("aria-describedby", feedback.id); to.setAttribute("aria-describedby", feedback.id);
  function range() {
    const now = Math.floor(Date.now() / 1000);
    const end = to.value ? Math.min(now, Math.floor(new Date(to.value + "T23:59:59").getTime() / 1000)) : now;
    const start = from.value ? Math.floor(new Date(from.value + "T00:00:00").getTime() / 1000) : Math.max(0, end - 31 * 86400);
    if (start > end || end - start > 31 * 86400) throw new Error("请选择开始不晚于结束、跨度最多 31 天且不晚于今天的日期范围。");
    const today = new Date(); const localToday = [today.getFullYear(), String(today.getMonth() + 1).padStart(2, "0"), String(today.getDate()).padStart(2, "0")].join("-");
    if (from.value > localToday || to.value > localToday) throw new Error("日期不能晚于今天。");
    return new URLSearchParams({after: String(start), before: String(end)});
  }
  const exportButton = button("导出 CSV", async () => {
    let url;
    try { url = "/admin/api/v1/audit/export.csv?" + range(); }
    catch (error) { feedback.textContent = error.message; return; }
    exportButton.disabled = true; feedback.textContent = "正在下载…";
    try {
      const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) throw new Error("导出失败，请检查日期范围或重新登录后重试。");
      const objectUrl = URL.createObjectURL(await response.blob());
      const link = node("a"); link.href = objectUrl; link.download = "cred-broker-audit.csv"; link.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      feedback.textContent = "CSV 已下载。";
    } catch (error) { feedback.textContent = error.message; }
    finally { exportButton.disabled = false; }
  }, "quiet");
  for (const input of [from, to]) input.addEventListener("change", () => {
    try { range(); feedback.textContent = "日期有效，可导出 CSV。"; }
    catch (error) { feedback.textContent = error.message; }
  });
  dateRange.append(fromLabel, from, toLabel, to, exportButton, feedback);
  function draw() {
    holder.replaceChildren();
    const entries = audit.filter(entry => {
      const connection = connections.find(item => String(item.id) === String(entry.connection_id));
      const outcome = outcomes[entry.outcome] || entry.outcome;
      return (filters.audit.outcome === "全部" || outcome === filters.audit.outcome) &&
        [entry.client, entry.action, actionNames[entry.action], connection?.label, entry.resource, outcome].join(" ").toLowerCase().includes(filters.audit.query.trim().toLowerCase());
    });
    resultFilter.replaceChildren(chips(["全部", "成功", "已拒绝", "失败"], filters.audit.outcome, value => {filters.audit.outcome = value; draw();}, "操作结果"));
    if (!entries.length) return empty(audit.length ? "没有匹配的操作记录。" : "暂无操作记录。", holder);
    const wrap = node("div", undefined, "table-wrap"); wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "操作记录表格");
    const table = node("table"); const head = table.createTHead().insertRow();
    for (const label of ["时间", "客户端", "操作", "连接", "资源", "结果"]) {const cell = node("th", label); cell.scope = "col"; head.append(cell);}
    const body = table.createTBody();
    for (const entry of entries) {
      const row = body.insertRow(); const connection = connections.find(item => String(item.id) === String(entry.connection_id));
      for (const value of [date(entry.ts), entry.client, actionNames[entry.action] || entry.action, connection?.label || entry.connection_id, entry.resource, outcomes[entry.outcome] || entry.outcome]) row.append(node("td", value ?? "—"));
    }
    wrap.append(table); holder.append(node("p", entries.length + " 条操作记录", "result-count"), wrap);
  }
  toolbar.append(searchField("搜索操作记录", "搜索客户端、操作或资源", filters.audit.query, value => {filters.audit.query = value; draw();}), resultFilter, dateRange);
  content.append(toolbar, holder); draw();
}

async function render(focus = false) {
  const id = ++revision;
  pageController?.abort(); pageController = new AbortController();
  const signal = pageController.signal;
  const refresh = byId("refresh-page"); if (refresh) refresh.disabled = true;
  const hash = location.hash.slice(1);
  const page = Object.hasOwn(pages, hash) ? hash : "catalog";
  for (const link of document.querySelectorAll("nav a")) {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const title = byId("title");
  title.textContent = pages[page][0];
  byId("intro").textContent = pages[page][1];
  if (byId("breadcrumb")) byId("breadcrumb").textContent = pages[page][0];
  byId("page-action").hidden = true;
  if (focus) title.focus();
  content.setAttribute("aria-busy", "true");
  content.replaceChildren(node("p", "正在加载…", "loading-state"));
  try {
    const [data, {connections}] = await Promise.all([
      page === "catalog" ? api("/providers", "GET", undefined, signal) : page === "grants" || page === "audit" ? api("/" + page, "GET", undefined, signal) : {},
      api("/connections", "GET", undefined, signal),
    ]);
    if (id !== revision) return;
    content.replaceChildren();
    if (byId("nav-connections-count")) {
      byId("nav-connections-count").textContent = connections.length;
      byId("nav-connections-count").hidden = connections.length === 0;
    }
    if (page === "grants" && byId("nav-grants-count")) {
      const activeGrants = data.grants.filter(grant => ["有效", "即将过期"].includes(grantState(grant))).length;
      byId("nav-grants-count").textContent = activeGrants;
      byId("nav-grants-count").hidden = activeGrants === 0;
    }
    if (page === "catalog") renderCatalog(data, connections);
    else if (page === "connections") renderConnections(connections);
    else if (page === "grants") renderGrants(data.grants, connections);
    else renderAudit(data.audit, connections);
  } catch (error) {
    if (id !== revision) return;
    const message = node("p", error instanceof Error ? error.message : "无法加载数据。");
    message.setAttribute("role", "alert");
    content.replaceChildren(message, button("重试", () => render(), "quiet"));
  } finally {
    if (id === revision) {
      content.setAttribute("aria-busy", "false");
      if (refresh) refresh.disabled = false;
    }
  }
}

byId("refresh-page")?.addEventListener("click", () => render());
window.addEventListener("hashchange", () => render(true));
const oauthResult = new URLSearchParams(location.search).get("google");
const oauthMessages = {connected: "Gmail 连接已完成，可在「我的连接」中测试。", failed: "Gmail 授权失败，请重新连接。", cancelled: "已取消 Gmail 授权。"};
if (Object.prototype.hasOwnProperty.call(oauthMessages, oauthResult)) {
  notice(oauthMessages[oauthResult]);
  history.replaceState(null, "", location.pathname + location.hash);
}
void render();
