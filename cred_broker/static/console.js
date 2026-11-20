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
let working = false;

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

async function api(path, method = "GET", body) {
  const response = await fetch("/admin/api/v1" + path, {
    method, credentials: "same-origin", cache: "no-store",
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
  element.textContent = text;
  element.hidden = !text;
}

function fail(error) {
  dialogError.textContent = error instanceof Error ? error.message : "操作失败，请重试。";
  dialogError.hidden = false;
}

function showDialog(title) {
  dialogContent.replaceChildren();
  dialogError.hidden = true;
  byId("dialog-title").textContent = title;
  if (!dialog.open) dialog.showModal();
}

function closeDialog() {
  if (!working) dialog.close();
}

byId("close-dialog").addEventListener("click", closeDialog);
dialog.addEventListener("cancel", (event) => {
  if (working) event.preventDefault();
});
dialog.addEventListener("close", () => dialogContent.replaceChildren());

function submitForm(form, text, action) {
  const submit = node("button", text);
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (working) return;
    working = true;
    submit.disabled = true;
    dialogError.hidden = true;
    try { await action(new FormData(form)); }
    catch (error) { fail(error); }
    finally { working = false; submit.disabled = false; }
  });
}

function field(form, name, label, options = {}) {
  const wrapper = node("label", label);
  const input = node(options.multiline ? "textarea" : "input");
  if (!options.multiline) input.type = options.type || "text";
  input.name = name;
  input.required = options.required !== false;
  input.autocomplete = "off";
  input.spellcheck = false;
  if (options.placeholder) input.placeholder = options.placeholder;
  if (options.value !== undefined) input.value = options.value;
  if (options.min !== undefined) input.min = options.min;
  if (options.max !== undefined) input.max = options.max;
  wrapper.append(input);
  form.append(wrapper);
  return input;
}

function createConnection(provider, oauthEnabled) {
  showDialog("连接 " + provider.name);
  if (provider.id === "gmail" && !oauthEnabled) {
    dialogContent.append(node("p", "请管理员配置 CB_GOOGLE_CLIENT_ID、CB_GOOGLE_CLIENT_SECRET 和 CB_GOOGLE_REDIRECT_URI。"));
    return;
  }
  const form = node("form");
  field(form, "label", "连接名称");
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
  submitForm(form, provider.id === "gmail" ? "前往 Google 授权" : "创建连接", async (data) => {
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
    dialog.close();
    location.hash = "connections";
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

function empty(text) {
  content.append(node("p", text, "empty"));
}

function renderCatalog({providers, google_oauth_enabled: oauthEnabled}, connections) {
  const grid = node("div", undefined, "grid");
  for (const [id, defaultName, icon] of catalog) {
    const provider = providers.find((item) => item.id === id);
    const supported = Boolean(provider?.supported) && id !== "feishu" && id !== "instagram";
    const item = card(defaultName, supported ? provider.description : "");
    const mark = node("img", undefined, "provider-icon");
    mark.src = "/static/icons/" + id + ".svg";
    mark.alt = "";
    mark.width = 44;
    mark.height = 44;
    item.prepend(mark);
    if (supported) {
      const actions = node("div", undefined, "actions");
      actions.append(button("连接", () => createConnection(provider, oauthEnabled)));
      const existing = connections.filter((connection) => connection.provider === id);
      if (existing.length === 1) actions.append(button("测试", () => testConnection(existing[0]), "quiet"));
      if (existing.length > 1) {
        actions.append(button("选择连接并测试", () => {
          showDialog("测试 " + defaultName);
          const choices = node("div", undefined, "list");
          for (const connection of existing) choices.append(button(connection.label, () => testConnection(connection), "quiet"));
          dialogContent.append(choices);
        }, "quiet"));
      }
      item.append(actions);
    } else item.append(node("span", "暂不支持", "badge"));
    grid.append(item);
  }
  content.append(grid);
}

async function testConnection(connection) {
  if (working) return;
  showDialog("测试 " + connection.label);
  working = true;
  dialogContent.append(node("p", "正在测试连接…"));
  try {
    const {ok} = await api("/connections/" + encodeURIComponent(connection.id) + "/test", "POST");
    dialogContent.replaceChildren(node("p", ok ? "连接测试通过。" : "连接测试未通过，请检查服务配置和凭证。"));
  } catch (error) {
    dialogContent.replaceChildren();
    fail(error);
  } finally { working = false; }
}

function confirmAction(title, description, action, success) {
  showDialog(title);
  const form = node("form");
  form.append(node("p", description));
  submitForm(form, "确认", async () => {
    await action();
    dialog.close();
    await render();
    notice(success);
  });
  dialogContent.append(form);
}

function renderConnections(connections) {
  if (!connections.length) return empty("还没有连接。请在连接目录中选择服务。");
  const list = node("div", undefined, "list");
  for (const connection of connections) {
    const service = catalog.find(([id]) => id === connection.provider);
    const item = card(connection.label, service?.[1] || connection.provider);
    const resources = connection.resources.map((entry) => entry.label).join("、");
    item.append(node("p", resources ? "资源：" + resources : "暂无可授权资源。"));
    const actions = node("div", undefined, "actions");
    actions.append(
      button("测试连接", () => testConnection(connection), "quiet"),
      button("删除连接", () => confirmAction("删除连接", "确定删除「" + connection.label + "」？", () => api("/connections/" + encodeURIComponent(connection.id), "DELETE"), "连接已删除。"), "danger"),
    );
    item.append(actions);
    list.append(item);
  }
  content.append(list);
}

function showToken(token) {
  showDialog("网关令牌已签发");
  dialogContent.append(node("p", "令牌仅在此窗口显示一次。请复制并保管，关闭后无法再次查看。"));
  const label = node("label", "网关令牌");
  const input = node("input", undefined, "secret-value");
  input.readOnly = true;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.value = token;
  label.append(input);
  const status = node("p");
  status.setAttribute("role", "status");
  dialogContent.append(label, button("复制令牌", async () => {
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = "已复制。";
    } catch {
      input.select();
      status.textContent = "无法访问剪贴板，请手动复制选中的令牌。";
    }
  }), status);
  input.focus();
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
  field(form, "client", "AI 客户端名称");
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
  submitForm(form, "签发令牌", async (data) => {
    const client = data.get("client").trim();
    const resources = data.getAll("resource");
    const tools = data.getAll("tool");
    if (!client || !resources.length || !tools.length) throw new Error("请填写客户端名称，并至少选择一个资源和一个工具。");
    const {token} = await api("/grants", "POST", {client, connection_id: select.value, resources, tools, ttl_seconds: Number(data.get("ttl"))});
    showToken(token);
    void render();
  });
  dialogContent.append(form);
}

function date(seconds) {
  return seconds == null ? "—" : new Date(seconds * 1000).toLocaleString("zh-CN");
}

function renderGrants(grants, connections) {
  const pageAction = byId("page-action");
  pageAction.textContent = "新建授权";
  pageAction.hidden = false;
  pageAction.disabled = !connections.length;
  pageAction.onclick = () => createGrant(connections);
  if (!grants.length) return empty(connections.length ? "还没有 AI 授权。" : "请先创建一个连接，再签发 AI 授权。");
  const list = node("div", undefined, "list");
  for (const grant of grants) {
    const connection = connections.find((item) => String(item.id) === String(grant.connection_id));
    const revoked = grant.revoked_at != null;
    const expired = grant.expires_at <= Date.now() / 1000;
    const item = card(grant.client, connection?.label || "连接已删除");
    item.append(
      node("span", revoked ? "已撤销" : expired ? "已过期" : "有效", "badge"),
      node("p", "资源：" + grant.resources.join("、")),
      node("p", "工具：" + grant.tools.join("、")),
      node("p", "到期时间：" + date(grant.expires_at)),
    );
    if (!revoked && !expired) item.append(button("撤销授权", () => confirmAction("撤销授权", "撤销后该令牌将立即失效。", () => api("/grants/" + encodeURIComponent(grant.id) + "/revoke", "POST"), "授权已撤销。"), "danger"));
    list.append(item);
  }
  content.append(list);
}

function renderAudit(audit, connections) {
  if (!audit.length) return empty("暂无操作记录。");
  const wrap = node("div", undefined, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "操作记录表格");
  const table = node("table");
  const head = table.createTHead().insertRow();
  for (const label of ["时间", "客户端", "操作", "连接", "资源", "结果"]) {
    const cell = node("th", label);
    cell.scope = "col";
    head.append(cell);
  }
  const body = table.createTBody();
  const outcomes = {ok: "成功", success: "成功", denied: "已拒绝", error: "失败", failed: "失败"};
  for (const entry of audit) {
    const row = body.insertRow();
    const connection = connections.find((item) => String(item.id) === String(entry.connection_id));
    for (const value of [date(entry.ts), entry.client, entry.action, connection?.label || entry.connection_id, entry.resource, outcomes[entry.outcome] ?? entry.outcome]) {
      row.append(node("td", value ?? "—"));
    }
  }
  wrap.append(table);
  content.append(wrap);
}

async function render(focus = false) {
  const id = ++revision;
  const hash = location.hash.slice(1);
  const page = Object.hasOwn(pages, hash) ? hash : "catalog";
  for (const link of document.querySelectorAll("nav a")) {
    if (link.hash === "#" + page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const title = byId("title");
  title.textContent = pages[page][0];
  byId("intro").textContent = pages[page][1];
  byId("page-action").hidden = true;
  if (focus) title.focus();
  content.setAttribute("aria-busy", "true");
  content.replaceChildren(node("p", "正在加载…"));
  try {
    const [data, {connections}] = await Promise.all([
      page === "catalog" ? api("/providers") : page === "grants" || page === "audit" ? api("/" + page) : {},
      api("/connections"),
    ]);
    if (id !== revision) return;
    content.replaceChildren();
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
    if (id === revision) content.setAttribute("aria-busy", "false");
  }
}

window.addEventListener("hashchange", () => render(true));
const oauthResult = new URLSearchParams(location.search).get("google");
const oauthMessages = {connected: "Gmail 连接已完成，可在「我的连接」中测试。", failed: "Gmail 授权失败，请重新连接。", cancelled: "已取消 Gmail 授权。"};
if (Object.prototype.hasOwnProperty.call(oauthMessages, oauthResult)) {
  notice(oauthMessages[oauthResult]);
  history.replaceState(null, "", location.pathname + location.hash);
}
void render();
