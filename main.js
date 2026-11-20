import { View, div } from "gpui-kit";
import { h_flex, v_flex, Input, InputState, Textarea, TextareaState } from "gpui-base";
import { appHome, checking, closeOverlay, exec, focused, pickFolder, props, readTextFile, run, theme } from "herdr";

const MOD = "herdr-team-coordinator";
const stopDrag = (_event, cx) => cx.stop_propagation();
const pages = [["runs", "运行与恢复"], ["start", "启动团队"], ["settings", "团队设置"]];
const roleLabels = { lead: "主控", master: "主控", developer: "开发", reviewer: "审查", specialist: "攻坚" };
const statusLabels = {
  running: "运行中", active: "进行中", online: "可用", available: "可用", paused: "已暂停",
  done: "已完成", completed: "已完成", accepted: "已接单", queued: "排队中", pending: "待处理",
  blocked: "受阻", stopped: "已停止", offline: "离线", idle: "空闲", working: "忙碌", lost: "会话失联",
  waiting: "等待中", ready: "待接单", held: "持有", released: "已交还", unknown: "原因待核实",
  unverified: "待核实", uncertain: "投递结果未知", invalidated: "已失效", sent: "已发送",
  review: "审查中", passed: "已通过", failed: "未通过", superseded: "已有后续版本",
  live: "可用", bound: "已绑定", launched: "已启动", offered: "待接单", yielded: "已让权",
  handed_off: "已交接", obsolete: "已撤销", historic: "已归档", delivered: "已投递", needs_decision: "旧任务待处理",
  awaiting_bind: "待绑定已有会话", stale_delivery: "旧投递待核对", retrying: "等待重启", detached: "调度器运行中", starting: "正在启动",
};
const label = value => statusLabels[value] ?? value ?? "待查询";
const rows = value => Array.isArray(value) ? value : [];
const pathPart = () => `${appHome().replaceAll("\\", "/")}/mods/${MOD}`;
const plain = value => typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");
const date = value => value ? new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value).toLocaleString() : "";

async function findPython() {
  const setting = await readTextFile(`${pathPart()}/python-path.json`);
  const configured = setting ? JSON.parse(setting).python : "";
  const candidates = configured ? [[configured, []]] : [["py.exe", ["-3"]], ["python.exe", []], ["python3.exe", []]];
  for (const [program, prefix] of candidates) {
    try {
      const probe = await exec(program, [...prefix, "-B", "-c", "import sys; print(sys.version_info >= (3, 10))"], { timeout: 4000 });
      if (probe.code === 0 && probe.stdout.trim() === "True") return [program, prefix];
    } catch {
      // Try the next registered launcher.
    }
  }
  throw Error(configured ? "已配置的 Python 无法启动或低于 3.10。" : "未找到 Python 3.10 或更高版本。请运行安装脚本并指定 Python 路径。");
}

function button(t, id, title, action, tone = "normal") {
  const primary = tone === "primary" && action;
  const selected = tone === "selected" && action;
  return h_flex().id(id).min_h(31).px(10).gap(5).flex_none().items_center().justify_center().rounded(6)
    .border_1().border_color(primary ? t.solid : selected ? t.accent : t.border)
    .bg(primary ? t.solid : selected ? t.selected : t.surface)
    .text_color(primary ? t.solid_foreground : action ? t.foreground : t.dim).text_size(13).child(title)
    .when(Boolean(action), el => el.cursor_pointer().hover(s => primary ? s.opacity(0.85) : s.bg(t.hover))
      .on_mouse_down("left", stopDrag).on_click(action));
}

function text(t, value, options = {}) {
  return div().min_w(0).text_size(options.size ?? 13)
    .text_color(options.color ?? t.foreground).when(options.bold, el => el.font_semibold())
    .child(plain(value));
}

const hint = (t, value) => text(t, value, { color: t.muted, size: 12 });
const heading = (t, value) => text(t, value, { size: 15, bold: true });
const group = (t, title) => v_flex().gap(8).pb(14).border_b(1).border_color(t.border).child(heading(t, title));
const actions = () => h_flex().gap(7).flex_wrap().items_center();
const item = (t, title) => v_flex().gap(5).px(10).py(8).rounded(6).bg(t.surface).child(text(t, title, { bold: true }));
const terminalAddress = term => term.address ?? term.terminal ?? term.id;
const terminalInstance = term => term.instance ?? term.instance_id ?? term.started_at;
const terminalAgent = term => term.agent ?? term.app ?? term.program?.name;
const terminalLabel = term => [terminalAddress(term), terminalAgent(term), label(term.status ?? term.state)].filter(Boolean).join(" · ");

export default class TeamCoordinator extends View {
  init(_props, cx) {
    this.overlay = props()?.overlay;
    this.role = props()?.role;
    this.page = "runs";
    this.data = null;
    this.background = null;
    this.error = "";
    this.message = "";
    this.busy = false;
    this.settingsEditing = false;
    this.editingRole = null;
    this.editingTeam = null;
    this.preview = null;
    this.launchMembers = {};
    this.expanded = {};
    if (checking() || this.role === "button") return;

    this.chatDraft = TextareaState.new({ placeholder: "告诉团队你想做什么，或给当前任务追加条件", rows: 2 });
    this.chatTarget = "";
    this.chatReply = null;
    this.chatAttempt = null;
    this.goal = TextareaState.new({ placeholder: "希望团队完成什么？", rows: 4 });
    this.criteria = TextareaState.new({ placeholder: "可选，例如必须完成的交付物或测试", rows: 2 });
    this.project = InputState.new({ placeholder: "本机项目目录", value: focused()?.cwd ?? "" });
    this.config = TextareaState.new({ placeholder: "团队配置 JSON", rows: 11 });
    this.roleFields = {
      name: InputState.new({ placeholder: "角色名称" }),
      duties: TextareaState.new({ placeholder: "角色职责", rows: 3 }),
      deliverables: TextareaState.new({ placeholder: "交付要求", rows: 2 }),
      reporting: TextareaState.new({ placeholder: "回报约定", rows: 2 }),
    };
    this.teamNameField = InputState.new({ placeholder: "团队名称" });
    this.recoveryEvidence = Object.fromEntries([
      ["terminal", "终端实例"], ["processes", "后台进程"], ["files", "改动文件"],
      ["handoff", "交接报告"], ["external", "外部操作凭据"],
    ].map(([key, title]) => [key, { title, state: InputState.new() }]));
    this.deliveryEvidence = InputState.new({ placeholder: "记录实际看到的消息或草稿" });
    this.job(cx, () => this.reload());
    cx.timer.every(15000, cx => {
      if (!this.busy && this.page === "runs") this.job(cx, async () => {
        this.background = await this.api("status");
        if (this.background?.running || this.background?.scheduler || this.background?.pid || this.background?.state === "running") {
          this.setDashboard(await this.api("dashboard", { run_id: this.selectedRun, before: this.chatBefore }));
        }
      }, true);
    });
  }

  async api(action, payload = {}) {
    this.python ??= await findPython();
    const [program, prefix] = this.python;
    const arg = JSON.stringify(payload);
    if (arg.length > 23000) throw Error("这次提交过长，请缩短输入后再试。");
    const args = [...prefix, "-B", "-X", "utf8", `${pathPart()}/coord.py`, "local", action, "--json", arg];
    const result = await exec(program, args, { timeout: 150000 });
    let response;
    try { response = JSON.parse(result.stdout); }
    catch { throw Error(result.stderr.trim() || "协调器未返回有效数据。"); }
    if (result.code !== 0 || response.ok === false || response.error) {
      const error = response.error;
      throw Error([error?.message ?? error ?? "协调器操作失败", response.next ?? response.next_step ?? error?.next_step].filter(Boolean).join("\n"));
    }
    return response.result ?? response.data ?? response;
  }

  job(cx, fn, quiet = false) {
    if (this.busy) return;
    this.busy = true;
    if (!quiet) { this.error = ""; this.message = ""; }
    cx.notify();
    cx.spawn(async cx => {
      try { await fn(); }
      catch (error) { if (!quiet) this.error = String(error.message ?? error); }
      this.busy = false;
      cx.notify();
    });
  }

  async reload() {
    this.setDashboard(await this.api("dashboard", { run_id: this.selectedRun, before: this.chatBefore }));
    this.background = await this.api("status");
  }

  setDashboard(data) {
    const previous = rows(this.data?.runs).find(row => row.id === this.selectedRun) ?? this.data?.runs?.[0];
    const current = rows(data?.runs).find(row => row.id === this.selectedRun) ?? data?.runs?.[0];
    if (current?.id !== previous?.id || current?.pause?.created !== previous?.pause?.created) this.clearRecoveryEvidence();
    this.data = data;
  }

  clearRecoveryEvidence() {
    for (const { state } of Object.values(this.recoveryEvidence)) state.set_value("");
    this.expanded.recoverGrant = null;
  }

  stoppedEvidence() {
    return { stopped: true, ...Object.fromEntries(Object.entries(this.recoveryEvidence)
      .map(([key, { state }]) => [key, state.value().trim()])) };
  }

  command(cx, action, payload, message, refresh = true) {
    this.job(cx, async () => {
      const result = await this.api(action, payload);
      if (refresh) await this.reload();
      this.message = typeof message === "function" ? message(result) : message;
    });
  }

  field(t, title, state, multi = false, height = 32) {
    return v_flex().gap(5).min_w(0).child(hint(t, title))
      .child(div().h(height).w_full().rounded(6).border_1().border_color(t.input).bg(t.background)
        .on_mouse_down("left", stopDrag).px(8).child((multi ? Textarea.new(state) : Input.new(state))
          .size_full().text_size(13).text_color(t.foreground)));
  }

  backgroundControls(t) {
    const status = this.background;
    const state = status?.running ? "running" : status?.phase ?? "unknown";
    const detail = status?.scheduler_pid ?? status?.pid;
    return h_flex().min_h(42).items_center().justify_between().gap(10).px(14).py(5).bg(t.chrome)
      .child(hint(t, `后台 ${label(state)}${detail ? ` · ${detail}` : ""}${status?.supervisor_pid ? ` · 监督 ${status.supervisor_pid}` : ""}`).truncate())
      .child(actions()
        .child(button(t, "background-start", "启动后台", this.busy ? null : (_e, cx) => this.command(cx, "start_daemon", {}, "已请求启动后台。")))
        .child(button(t, "background-stop", "停止后台", this.busy ? null : (_e, cx) => this.job(cx, async () => {
          await this.api("stop");
          this.background = await this.api("status");
          this.message = "已请求有序停机。";
        })))
        .child(button(t, "background-restart", "重启后台", this.busy ? null : (_e, cx) => this.command(cx, "restart", {}, "已请求重启后台。"))));
  }

  decision(t, runData, question) {
    const questionId = question.id ?? question.question_id;
    const scope = typeof question.scope === "object" ? question.scope : null;
    const title = { integration: "开始联调", acceptance: "验收并结束任务", delete: "删除", permission: "扩大权限", provider: "切换服务商" };
    const purpose = question.purpose ?? question.kind;
    const revision = question.revision ?? runData.revision;
    const pending = scope || question.channel === "local" || question.local_required ||
      purpose === "acceptance" && (runData.acceptance_mode ?? runData.acceptance) === "local";
    const row = item(t, title[purpose] ?? purpose ?? "待确认").child(hint(t, `目标版本 ${revision} · ${questionId}`));
    if (question.question ?? question.prompt ?? question.text) row.child(text(t, question.question ?? question.prompt ?? question.text));
    if (question.artifact_digest ?? question.digest) row.child(hint(t, `成果摘要 ${question.artifact_digest ?? question.digest}`));
    if (scope) for (const [key, field] of [["target", "对象"], ["extent", "范围"], ["impact", "影响"]]) {
      if (scope[key]) row.child(hint(t, `${field} · ${plain(scope[key])}`));
    }
    const decide = (cx, accepted) => this.command(cx, "decide", {
      run_id: runData.id, decision_id: questionId, accepted, revision,
    }, accepted ? "已核对本次决定。" : "已处理本次决定。");
    return row.child(pending ? actions()
      .child(button(t, `decline-${questionId}`, "拒绝…", this.busy ? null : (_e, cx) => decide(cx, false)))
      .child(button(t, `approve-${questionId}`, "打开本地确认…", this.busy ? null : (_e, cx) => decide(cx, true), "primary"))
      : hint(t, "可在主控聊天中回复这个待决问题。"));
  }

  binding(t, runData, member) {
    const bindingID = member.id;
    const terminal = member.terminal ?? member.address;
    const role = member.kind ?? member.role;
    const memberName = member.name ?? member.member_id ?? member.role_id ?? bindingID;
    const statusParts = [
      terminal,
      member.provider ?? member.agent,
      label(member.launch_state),
      member.health && member.health !== "unknown" ? label(member.health) : null,
      member.terminal_status ? label(member.terminal_status) : null,
    ].filter(Boolean);
    return item(t, `${memberName} · ${roleLabels[role] ?? plain(role)}`)
      .child(hint(t, statusParts.join(" · ")))
      .when(Boolean(member.launch_evidence), row => row.child(hint(t, member.launch_evidence)))
      .child(actions()
        .child(button(t, `focus-${bindingID}`, "打开会话", terminal ? () => run("agent.focus", { terminal }) : null))
        .when(!["lead", "master"].includes(role) && runData.state !== "accepted", row => row.child(button(t, `rebind-${bindingID}`,
          terminal ? "更换会话" : "绑定已有会话", this.busy ? null : (_e, cx) => {
            this.expanded.recover = { id: bindingID, term: null }; cx.notify();
          })))
        .child(button(t, `pair-${bindingID}`, "获取配对指令", this.busy || !terminal || member.active === 0 ? null : (_e, cx) => this.job(cx, async () => {
          const info = await this.api("bootstrap", { run_id: runData.id, binding_id: bindingID });
          this.expanded.bootstrap = { id: bindingID, ...info };
          this.message = "请把配对指令交给这一个会话。";
        }))))
      .when(this.expanded.bootstrap?.id === bindingID, row => {
        const info = this.expanded.bootstrap;
        return row.child(v_flex().gap(6).p(8).rounded(6).bg(t.background).border_1().border_color(t.border)
          .child(hint(t, "接入命令"))
          .child(text(t, info.command, { size: 12 }))
          .child(hint(t, "完整接入提示"))
          .child(text(t, info.prompt, { size: 12 }))
          .child(actions()
            .child(button(t, `copy-${bindingID}`, "复制指令", (_e, cx) => {
              cx.write_to_clipboard(info.prompt || info.command);
              this.expanded.bootstrap = null;
              this.message = "配对指令已复制。";
              cx.notify();
            }, "primary"))
            .child(button(t, `hide-${bindingID}`, "关闭", (_e, cx) => { this.expanded.bootstrap = null; cx.notify(); }))));
      })
      .when(this.expanded.recover?.id === bindingID, row => row.child(this.rebindPicker(t, runData, member, false)));
  }

  delivery(t, runData, entry) {
    const id = entry.id ?? entry.delivery_id;
    const state = entry.state ?? entry.status;
    const recipient = rows(runData.bindings).find(member => member.id === entry.binding_id);
    const autoReady = Boolean(recipient?.created_by_plugin);
    const row = item(t, `${recipient?.role_id ?? entry.recipient ?? entry.target ?? id} · ${label(state)}`);
    if (entry.text) row.child(text(t, entry.text));
    const reason = entry.reason ?? entry.last_error ?? entry.error ?? entry.evidence;
    if (reason) row.child(hint(t, reason));
    if (entry.next_check_at ?? entry.next_check ?? entry.not_before) row.child(hint(t, `下次核查 ${date(entry.next_check_at ?? entry.next_check ?? entry.not_before)}`));
    if (entry.owner || entry.escalation) row.child(hint(t, [entry.owner, entry.escalation].filter(Boolean).join(" · ")));
    if (entry.possible_delivery || entry.stale || entry.maybe_delivered) row.child(hint(t, "旧指令可能已经送达。"));
    if (state === "queued" && recipient?.terminal && runData.revision === entry.revision) {
      row.child(hint(t, autoReady
        ? "等待目标会话空闲后自动投递（也可核对草稿后立即手动推送）。"
        : "已有会话不自动注入，成员可用 coord status 读取，或核对草稿后手动推送。"));
      row.child(button(t, `approve-send-${id}`, "核对草稿后投递…", this.busy ? null : (_e, cx) => this.command(cx,
        "approve_delivery", { run_id: runData.id, delivery_id: id }, "已登记投递决定。")));
    }
    if (["stale_delivery", "unknown"].includes(state)) {
      if (this.expanded.delivery !== id) return row.child(button(t, `verify-send-${id}`, "登记核查结果", (_e, cx) => {
        this.expanded.delivery = id; this.deliveryEvidence.set_value(""); cx.notify();
      }));
      const resolve = (cx, received) => this.command(cx, "resolve_delivery", {
        run_id: runData.id, delivery_id: id, received, evidence: this.deliveryEvidence.value().trim(),
      }, "已记下核查证据。");
      row.child(this.field(t, "核查证据", this.deliveryEvidence)).child(actions()
        .child(button(t, `verify-no-${id}`, "核实未收到", this.busy ? null : (_e, cx) => resolve(cx, false)))
        .child(button(t, `verify-yes-${id}`, "核实已收到", this.busy ? null : (_e, cx) => resolve(cx, true), "primary")));
    }
    return row;
  }

  recovery(t, runData) {
    const lead = rows(runData.bindings ?? runData.members).find(x => ["lead", "master"].includes(x.kind ?? x.role) && x.active !== 0);
    const content = group(t, "主控恢复");
    if (!lead) return content.child(hint(t, "未找到主控绑定。"));
    content.child(hint(t, `${lead.terminal ?? "未绑定"} · ${label(runData.master_state ?? lead.health ?? lead.state ?? lead.status)}`));
    if (runData.state === "accepted") return content;
    if (this.expanded.recover?.id !== lead.id) return content.child(button(t, "recover", "选择要绑定的主控会话", this.busy ? null : (_e, cx) => {
      this.expanded.recover = { id: lead.id, term: null }; cx.notify();
    }));
    return content.child(this.rebindPicker(t, runData, lead, true));
  }

  rebindPicker(t, runData, binding, master) {
    const candidate = this.expanded.recover.term;
    const busyTerminals = new Set(rows(this.data.runs)
      .filter(row => row.state !== "accepted")
      .flatMap(row => rows(row.bindings))
      .filter(member => member.active !== 0 && member.id !== binding.id && member.terminal)
      .map(member => member.terminal));
    const terminals = rows(this.data?.terminals).filter(term => terminalInstance(term) && terminalAddress(term) &&
      term.state === "live" && (!binding.provider || terminalAgent(term) === binding.provider) &&
      !busyTerminals.has(terminalAddress(term)) && !(term.labels?.["team.run"]));
    const choices = actions();
    for (const term of terminals) choices.child(button(t, `recover-${terminalAddress(term)}`, terminalLabel(term), (_e, cx) => {
      this.expanded.recover.term = term; cx.notify();
    }, candidate && terminalInstance(candidate) === terminalInstance(term) ? "selected" : "normal"));
    const content = v_flex().gap(8);
    if (!terminals.length) content.child(hint(t, "当前找不到能核验实例的同类会话。可在项目目录启动 CLI 后刷新。"));
    return content.child(choices).child(actions()
      .child(button(t, "recover-cancel", "取消", (_e, cx) => { this.expanded.recover = null; cx.notify(); }))
      .child(button(t, "recover-confirm", master ? "确认绑定并恢复主控" : "确认绑定成员", candidate && !this.busy ? (_e, cx) => this.command(cx,
      master ? "rebind_lead" : "rebind_member", {
        run_id: runData.id, binding_id: binding.id, terminal: terminalAddress(candidate), instance: terminalInstance(candidate),
      }, () => {
        this.expanded.recover = null;
        this.expanded.bootstrap = null;
        return "已绑定会话。请获取该会话的配对指令。";
      }) : null, "primary")));
  }

  renderChat(t, current) {
    const section = group(t, "团队对话");
    const tasks = rows(current.tasks);
    for (const msg of rows(current.conversation)) {
      const who = msg.origin === "panel" ? "你" : msg.sender;
      const row = item(t, `${who} → ${msg.recipient}`).child(text(t, msg.text));
      const task = tasks.find(it => it.id === msg.task_id);
      const confirmation = task?.task_version > 1 ? (task.ack_version === task.task_version ? " · 成员已确认" : " · 等待成员确认") : "";
      const progress = task ? `${task.title} · 第 ${task.task_version} 版 · ${label(task.state)}${confirmation}` : "";
      const effect = { dispatch: "已派工", "amend-task": "已更新要求", reply: "已回复", say: "已回应" }[msg.result?.action];
      row.child(hint(t, [effect ?? (msg.revision === current.revision ? "已记录" : "历史消息"), progress].filter(Boolean).join(" · ")));
      if (current.state !== "accepted") row.child(button(t, `reply-${msg.seq}`, "引用回复", (_e,cx) => {
        this.chatReply = msg;
        this.chatTarget = msg.origin === "panel" ? msg.recipient : msg.sender;
        cx.notify();
      }));
      section.child(row);
    }
    const history = rows(current.conversation);
    if (history.length === 12 || this.chatBefore) {
      const paging = actions();
      if (history.length === 12) paging.child(button(t, "chat-earlier", "更早消息", (_e,cx) => this.job(cx, async () => {
        this.chatBefore = history[0].seq;
        await this.reload();
      })));
      if (this.chatBefore) paging.child(button(t, "chat-latest", "最新消息", (_e,cx) => this.job(cx, async () => {
        this.chatBefore = null;
        await this.reload();
      })));
      section.child(paging);
    }
    if (current.state === "accepted") return section;
    const targets = actions();
    const members = rows(current.bindings);
    const selectedTarget = members.some(b => b.kind === "master" && b.role_id === this.chatTarget) ? "" : this.chatTarget;
    for (const [id, name] of [["", "团队主控"], ...members.filter(b => b.kind !== "master").map(b => [b.role_id, b.name || b.role_id])]) {
      targets.child(button(t, `chat-to-${id || "team"}`, name, (_e,cx) => {
        this.chatTarget = id; cx.notify();
      }, selectedTarget === id ? "selected" : "normal"));
    }
    section.child(targets);
    if (this.chatReply) section.child(button(t, "chat-clear-reply", `取消引用 · ${this.chatReply.text.slice(0,40)}`, (_e,cx) => {
      this.chatReply = null; cx.notify();
    }));
    section.child(this.field(t, "消息", this.chatDraft, true, 64));
    return section.child(button(t, "chat-send", "发送", this.busy ? null : (_e,cx) => this.job(cx, async () => {
      const body = this.chatDraft.value().trim();
      if (!body) throw Error("请填写消息。");
      const payload = { run_id: current.id, to: this.chatTarget, reply_to: this.chatReply?.id ?? null, text: body };
      const key = JSON.stringify(payload);
      if (this.chatAttempt?.key !== key) this.chatAttempt = { key, id: `panel-${Date.now()}-${Math.random().toString(36).slice(2)}` };
      await this.api("say", { ...payload, message_id: this.chatAttempt.id });
      if (this.chatDraft.value().trim() === body) this.chatDraft.set_value("");
      this.chatReply = null;
      this.chatAttempt = null;
      this.chatBefore = null;
      await this.reload();
    }), "primary"));
  }

  renderRuns(t) {
    const all = rows(this.data?.runs);
    if (!all.length) return v_flex().gap(9).child(heading(t, "还没有运行"))
      .child(button(t, "first-launch", "启动团队", (_e, cx) => { this.page = "start"; cx.notify(); }, "primary"));
    const current = all.find(x => x.id === this.selectedRun) ?? all[0];
    const selection = actions();
    for (const entry of all) selection.child(button(t, `run-${entry.id}`, entry.team_name ?? entry.name ?? entry.goal?.slice(0, 20) ?? entry.id, (_e, cx) => {
      this.chatBefore = null;
      this.chatTarget = "";
      this.chatReply = this.chatAttempt = null;
      this.chatDraft.set_value("");
      this.selectedRun = entry.id;
      this.job(cx, () => this.reload());
      this.expanded = {};
      this.clearRecoveryEvidence();
      cx.notify();
    }, entry.id === current.id ? "selected" : "normal"));
    const workflow = current.workflow ?? current;
    const title = group(t, "当前目标").child(text(t, current.goal))
      .child(hint(t, [current.root ?? current.project, `版本 ${current.revision}`, label(current.state ?? current.status)].filter(Boolean).join(" · ")));
    const paused = (current.state ?? current.status) === "paused";
    const emergency = paused && workflow.pause?.emergency;
    if (emergency) for (const { title: fieldTitle, state } of Object.values(this.recoveryEvidence)) {
      title.child(this.field(t, fieldTitle, state));
    }
    if (["active", "paused"].includes(current.state)) title.child(actions().child(button(t, "pause-resume",
      emergency ? "确认已停止写入并恢复" : paused ? "恢复调度" : "暂停调度", this.busy ? null : (_e, cx) => {
        const params = { run_id: current.id };
        if (!paused) params.reason = "用户从协调器本地面板暂停调度。";
        if (emergency) params.evidence = this.stoppedEvidence();
        this.command(cx, paused ? "resume" : "pause", params, paused ? "已请求恢复调度。" : "已请求暂停调度。");
      })));

    const panel = v_flex().gap(13).child(selection).child(title).child(this.renderChat(t, current));
    const questions = [...rows(current.questions), workflow.question ?? current.decision].filter(Boolean)
      .filter((row, index, all) => all.findIndex(other => (other.id ?? other.question_id) === (row.id ?? row.question_id)) === index)
      .filter(row => !row.state || ["pending", "open", "awaiting"].includes(row.state));
    if (questions.length) panel.child(group(t, "待决事项").children(questions.map(question => this.decision(t, current, question))));
    panel.child(this.recovery(t, current));

    const tasks = rows(current.tasks);
    const review = workflow.review ?? rows(current.reviews)[0];
    const work = group(t, "任务与审查");
    for (const task of tasks) work.child(item(t, `${task.title ?? task.name ?? task.id} · ${label(task.state ?? task.status)}`)
      .child(hint(t, [task.assignee ?? task.role_id, task.required ? "必做" : "", task.completion ?? task.reason].filter(Boolean).join(" · "))));
    const grants = rows(workflow.write_grants ?? current.grants);
    for (const grant of grants) {
      const holder = rows(current.bindings).find(member => member.id === grant.binding_id);
      const grantRow = item(t, `写入资格 · ${grant.role_id ?? grant.binding_id} · 第 ${grant.round} 轮`)
        .child(hint(t, [grant.path, holder?.terminal, holder?.instance].filter(Boolean).join(" · ")));
      if (paused && this.expanded.recoverGrant !== grant.id) grantRow.child(button(t, `recover-grant-${grant.id}`, "核对旧轮次并申请释放", this.busy ? null : (_e, cx) => {
        this.clearRecoveryEvidence();
        this.expanded.recoverGrant = grant.id;
        cx.notify();
      }));
      if (paused && this.expanded.recoverGrant === grant.id) {
        grantRow.child(hint(t, "原成员仍可回报时，由该轮次交还资格。仅在核实旧会话已退出且后台写入均已停止后提交。"));
        if (!emergency) for (const { title: fieldTitle, state } of Object.values(this.recoveryEvidence)) {
          grantRow.child(this.field(t, fieldTitle, state));
        }
        grantRow.child(actions().child(button(t, "cancel-grant-recovery", "取消", (_e, cx) => { this.clearRecoveryEvidence(); cx.notify(); }))
          .child(button(t, "confirm-grant-recovery", "确认旧轮次已停止写入…", this.busy ? null : (_e, cx) => this.job(cx, async () => {
            await this.api("recover_write", { run_id: current.id, grant_ids: [grant.id], evidence: this.stoppedEvidence() });
            this.expanded.recoverGrant = null;
            if (!emergency) this.clearRecoveryEvidence();
            await this.reload();
            this.message = "已按核查结果释放旧轮次资格。";
          }), "primary")));
      }
      work.child(grantRow);
    }
    if (review) work.child(item(t, `审查 ${review.id ?? ""} · ${label(review.state ?? review.status)}`)
      .child(hint(t, [review.revision ? `版本 ${review.revision}` : "", review.digest ?? review.artifact_digest, review.invalid_reason].filter(Boolean).join(" · "))));
    const accepted = workflow.acceptance_record ?? (typeof workflow.acceptance === "object" ? workflow.acceptance : null);
    if (accepted) work.child(hint(t,
      ["已验收", `${accepted.revision} 版`, date(accepted.created), accepted.digest].filter(Boolean).join(" · ")));
    if (!tasks.length && !review) work.child(hint(t, "等待主控派工。"));
    panel.child(work);

    const memberRows = rows(current.bindings ?? current.members);
    if (memberRows.length) panel.child(group(t, "会话").children(memberRows.map(member => this.binding(t, current, member))));
    const waits = rows(current.waits ?? workflow.write_waits);
    const deliveries = rows(current.deliveries);
    if (waits.length || deliveries.length) {
      const waiting = group(t, "投递与等待");
      for (const wait of waits) {
        const deps = [
          wait.depends_on_role ? `依赖角色 ${wait.depends_on_role}` : null,
          wait.depends_on_task ? `依赖任务 ${wait.depends_on_task}` : null,
          wait.depends_on_resource ? `依赖资源 ${wait.depends_on_resource}` : null,
        ].filter(Boolean).join(" · ");
        waiting.child(item(t, wait.reason ?? wait.kind ?? wait.id)
          .when(Boolean(deps), row => row.child(hint(t, deps)))
          .child(hint(t, [wait.owner_role ?? wait.owner, date(wait.next_check_at ?? wait.next_check), wait.escalation ?? wait.condition].filter(Boolean).join(" · "))));
      }
      for (const entry of deliveries.slice(0, 25)) waiting.child(this.delivery(t, current, entry));
      panel.child(waiting);
    }
    const audit = rows(current.audit);
    const unresolvedRequests = rows(current.open_requests);
    if (unresolvedRequests.length) panel.child(group(t, "待查请求").children(unresolvedRequests.map(entry =>
      hint(t, [entry.role_id, entry.action, entry.id, date(entry.created)].filter(Boolean).join(" · ")))));
    if (audit.length) panel.child(group(t, "记录").children(audit.slice(0, 12).map(entry =>
      hint(t, [date(entry.created ?? entry.at), entry.message ?? entry.action ?? entry.kind].filter(Boolean).join(" · ")))));
    return panel;
  }

  renderStart(t) {
    if (!this.project) return v_flex();
    const config = this.data?.config;
    const teams = rows(config?.teams).filter(team => !team.archived);
    const selected = teams.find(team => team.id === this.teamId) ?? teams[0];
    const acceptance = this.launchAcceptance ?? selected?.acceptance ?? "chat";
    const roles = new Map(rows(config?.roles).map(role => [role.id, role]));
    const terminals = rows(this.data?.terminals);
    const alreadyBound = new Set(rows(this.data?.runs)
      .filter(row => row.state !== "accepted")
      .flatMap(row => rows(row.bindings))
      .filter(member => member.active !== 0 && member.terminal)
      .map(member => member.terminal));
    const content = group(t, "新运行").child(actions().children(teams.map(team => button(t, `team-${team.id}`, team.name, (_e, cx) => {
      this.teamId = team.id; this.launchMembers = {}; this.launchAcceptance = null; cx.notify();
    }, selected?.id === team.id ? "selected" : "normal"))));
    if (!selected) return content.child(hint(t, "请在团队设置保存至少一个可用团队。"));
    content.child(this.field(t, "工作目录", this.project)).child(button(t, "choose-project", "选择目录", this.busy ? null : (_e, cx) => this.job(cx, async () => {
      const directory = await pickFolder({ prompt: "选择团队的本机工作目录" });
      if (directory) this.project.set_value(directory);
    }))).child(this.field(t, "目标", this.goal, true, 90)).child(this.field(t, "完成条件", this.criteria, true, 60));
    for (const member of rows(selected.members)) {
      const current = this.launchMembers[member.id];
      const terminalsForMember = terminals.filter(term => terminalInstance(term) && term.state === "live" && term.status !== "working" &&
        member.agent === terminalAgent(term) && !alreadyBound.has(terminalAddress(term)) && !term.labels?.["team.run"]);
      const row = item(t, `${roles.get(member.role)?.name ?? member.id} · ${member.agent} / ${member.model}`)
        .child(hint(t, `${member.write_allowed ? "已启用写入资格申请" : "协调器写入申请关闭"} · ${member.effort === "default" ? "CLI 默认思考强度" : member.effort}`));
      const choices = actions().child(button(t, `new-${member.id}`, member.agent === "dsh" ? "稍后绑定" : "新建会话", (_e, cx) => {
        delete this.launchMembers[member.id]; cx.notify();
      }, !current ? "selected" : "normal"));
      if (member.agent === "dsh") row.child(hint(t, "DSH 需绑定项目目录中的已有会话。"));
      for (const term of terminalsForMember) if (!Object.entries(this.launchMembers).some(([id, other]) =>
        id !== member.id && terminalAddress(other) === terminalAddress(term))) {
        choices.child(button(t, `existing-${member.id}-${terminalAddress(term)}`, terminalLabel(term), (_e, cx) => {
          this.launchMembers[member.id] = term; cx.notify();
        }, current && terminalAddress(current) === terminalAddress(term) ? "selected" : "normal"));
      }
      content.child(row.child(choices));
    }
    content.child(hint(t, "最终验收方式")).child(actions().children([["chat", "主控聊天验收"], ["local", "本地确认验收"]]
      .map(([key, value]) => button(t, `accept-mode-${key}`, value, (_e, cx) => {
        this.launchAcceptance = key; cx.notify();
      }, acceptance === key ? "selected" : "normal"))));
    if (Object.keys(this.launchMembers).length) content.child(hint(t, "已有会话保留当前 CLI 权限。绑定后从运行页获取配对指令。"));
    return content.child(button(t, "start-team", "确认启动团队", this.busy ? null : (_e, cx) => this.job(cx, async () => {
      const project = this.project.value().trim();
      const goal = this.goal.value().trim();
      if (!project || !goal) throw Error("请填写工作目录和目标。");
      const criteria = this.criteria.value().trim();
      const result = await this.api("start", {
        team_id: selected.id, project, goal: criteria ? `${goal}\n\n完成条件\n${criteria}` : goal, acceptance,
        members: Object.fromEntries(Object.entries(this.launchMembers).map(([id, term]) => [id, {
          terminal: terminalAddress(term), instance: terminalInstance(term),
        }])),
      });
      this.chatBefore = null;
      this.chatTarget = "";
      this.chatReply = this.chatAttempt = null;
      this.chatDraft.set_value("");
      this.selectedRun = result?.id ?? result?.run_id;
      this.page = "runs";
      this.expanded = {};
      await this.reload();
      this.message = "已登记团队运行。";
    }), "primary"));
  }

  renderSettings(t) {
    const config = this.data?.config;
    const panel = v_flex().gap(13);
    if (!config) return panel.child(hint(t, "尚未读取团队设置。"));
    const teams = rows(config.teams);
    const allRoles = rows(config.roles);
    const roles = new Map(allRoles.map(role => [role.id, role]));
    const agentProfiles = this.data?.platform?.agents ?? {};

    const overview = group(t, "现有团队");
    overview.child(button(t, "new-team", "新建团队", this.busy ? null : (_e, cx) => {
      const baseTeam = teams.find(item => !item.archived) ?? teams[0];
      const draft = JSON.parse(JSON.stringify(baseTeam));
      draft.id = "team-" + Date.now().toString(36);
      draft.name = "新团队";
      draft.archived = false;
      this.editingTeam = { isNew: true, data: draft };
      this.teamNameField.set_value(draft.name);
      cx.notify();
    }));
    for (const team of teams) {
      overview.child(item(t, `${team.name}${team.archived ? " · 已归档" : ""}`)
        .child(hint(t, `${team.acceptance === "local" ? "本地确认验收" : "主控聊天验收"} · ` + rows(team.members).map(member => `${roles.get(member.role)?.name ?? member.id} / ${member.agent} / ${member.model}${member.effort && member.effort !== "default" ? " (" + member.effort + ")" : ""}${member.write_allowed ? " · 可申请写入" : ""}`).join("  ·  ")))
        .child(actions()
          .child(button(t, `edit-team-${team.id}`, "编辑团队与模型", this.busy ? null : (_e, cx) => {
            this.editingTeam = { isNew: false, data: JSON.parse(JSON.stringify(team)) };
            this.teamNameField.set_value(team.name);
            cx.notify();
          }))
          .children(rows(team.members).filter(member => ["developer", "specialist"].includes(member.kind)).map(member =>
            button(t, `write-${team.id}-${member.id}`, `${roles.get(member.role)?.name ?? member.id} · ${member.write_allowed ? "关闭写入申请" : "启用写入申请…"}`,
              !this.busy && !this.settingsEditing ? (_e, cx) => {
                const next = JSON.parse(JSON.stringify(config));
                next.teams.find(row => row.id === team.id).members.find(row => row.id === member.id).write_allowed = !member.write_allowed;
                this.command(cx, "save_config", { config: next }, "写入申请设置已更新。后续启动采用此配置。");
              } : null)))));
    }
    if (this.editingTeam) {
      const draft = this.editingTeam.data;
      const box = v_flex().gap(8).p(10).rounded(6).bg(t.background).border_1().border_color(t.border)
        .child(heading(t, this.editingTeam.isNew ? "新建团队" : `编辑团队 · ${draft.id}`))
        .child(this.field(t, "团队名称", this.teamNameField))
        .child(hint(t, "默认验收方式"))
        .child(actions().children([["chat", "主控聊天验收"], ["local", "本地确认验收"]].map(([mode, title]) =>
          button(t, `team-accept-${mode}`, title, (_e, cx) => { draft.acceptance = mode; cx.notify(); }, draft.acceptance === mode ? "selected" : "normal"))));
      for (const member of rows(draft.members)) {
        const isLead = member.kind === "lead";
        const roleChoices = allRoles.filter(r => !r.archived && (isLead ? r.kind === "lead" : r.kind !== "lead"));
        const profile = agentProfiles[member.agent] ?? {};
        const models = rows(profile.models).length ? rows(profile.models) : [{ id: "default", label: "CLI 默认模型", efforts: [] }];
        const selectedModel = models.find(m => m.id === member.model) ?? models[0];
        const efforts = member.model === "default" ? ["default", "low", "medium", "high", "xhigh"] :
          rows(selectedModel?.efforts).length ? ["default", ...rows(selectedModel.efforts)] : ["default"];
        const mBox = item(t, `${member.id} · ${roleLabels[member.kind] ?? member.kind}`)
          .child(hint(t, "角色"))
          .child(actions().children(roleChoices.map(r => button(t, `m-role-${member.id}-${r.id}`, r.name, (_e, cx) => {
            member.role = r.id; member.kind = r.kind;
            if (!["developer", "specialist"].includes(member.kind)) member.write_allowed = false;
            cx.notify();
          }, member.role === r.id ? "selected" : "normal"))))
          .child(hint(t, "Agent CLI"))
          .child(actions().children(["codex", "antigravity", "dsh"].map(ag => {
            const avail = agentProfiles[ag]?.available;
            return button(t, `m-agent-${member.id}-${ag}`, `${ag}${avail === false ? "（未发现）" : ""}`, (_e, cx) => {
              member.agent = ag; member.model = "default"; member.effort = "default"; cx.notify();
            }, member.agent === ag ? "selected" : "normal");
          })))
          .child(hint(t, "模型"))
          .child(actions().children(models.slice(0, 14).map(mod => button(t, `m-mod-${member.id}-${mod.id}`, mod.label || mod.id, (_e, cx) => {
            member.model = mod.id; member.effort = "default"; cx.notify();
          }, member.model === mod.id ? "selected" : "normal"))))
          .child(hint(t, "思考强度"))
          .child(actions().children(efforts.map(eff => button(t, `m-eff-${member.id}-${eff}`, eff === "default" ? "CLI 默认" : eff, (_e, cx) => {
            member.effort = eff; cx.notify();
          }, (member.effort ?? "default") === eff ? "selected" : "normal"))));
        const mActions = actions();
        if (["developer", "specialist"].includes(member.kind)) {
          mActions.child(button(t, `m-write-${member.id}`, member.write_allowed ? "写入申请：已启用" : "写入申请：关闭", (_e, cx) => {
            member.write_allowed = !member.write_allowed; cx.notify();
          }, member.write_allowed ? "selected" : "normal"));
        }
        if (!isLead && draft.members.length > 2) {
          mActions.child(button(t, `m-del-${member.id}`, "移除成员", (_e, cx) => {
            draft.members = draft.members.filter(x => x.id !== member.id); cx.notify();
          }));
        }
        box.child(mBox.child(mActions));
      }
      box.child(actions()
        .child(button(t, "team-add-member", "添加成员", (_e, cx) => {
          const devRole = allRoles.find(r => !r.archived && r.kind === "developer") ?? allRoles.find(r => !r.archived && r.kind !== "lead");
          if (!devRole) return;
          draft.members.push({ id: "m-" + (draft.members.length + 1) + "-" + Date.now().toString(36).slice(-3), role: devRole.id, kind: devRole.kind, agent: "codex", model: "default", effort: "default", write_allowed: false });
          cx.notify();
        }))
        .child(button(t, "team-cancel", "取消", (_e, cx) => { this.editingTeam = null; cx.notify(); }))
        .child(button(t, "team-save", "保存团队配置", this.busy ? null : (_e, cx) => this.job(cx, async () => {
          const next = JSON.parse(JSON.stringify(config));
          draft.name = this.teamNameField.value().trim();
          const idx = next.teams.findIndex(item => item.id === draft.id);
          if (idx < 0) next.teams.push(draft); else next.teams[idx] = draft;
          await this.api("save_config", { config: next });
          this.editingTeam = null;
          await this.reload();
          this.message = "团队配置已保存。";
        }), "primary")));
      overview.child(box);
    }
    panel.child(overview);

    const roleGroup = group(t, "角色库");
    roleGroup.child(actions()
      .child(button(t, "new-role", "新建角色", (_e, cx) => {
        const draft = { id: "role-" + Date.now().toString(36), kind: "developer", name: "", duties: "", deliverables: "", reporting: "", revision: 1, archived: false };
        this.editingRole = { isNew: true, data: draft };
        this.roleFields.name.set_value(""); this.roleFields.duties.set_value(""); this.roleFields.deliverables.set_value(""); this.roleFields.reporting.set_value("");
        cx.notify();
      }))
      .children(allRoles.filter(r => !r.archived).map(role => button(t, `edit-role-${role.id}`, `编辑 ${role.name}`, (_e, cx) => {
        this.editingRole = { isNew: false, data: JSON.parse(JSON.stringify(role)) };
        this.roleFields.name.set_value(role.name ?? "");
        this.roleFields.duties.set_value(role.duties ?? "");
        this.roleFields.deliverables.set_value(role.deliverables ?? "");
        this.roleFields.reporting.set_value(role.reporting ?? "");
        cx.notify();
      }))));
    if (this.editingRole) {
      const roleDraft = this.editingRole.data;
      roleGroup.child(v_flex().gap(8).p(10).rounded(6).bg(t.background).border_1().border_color(t.border)
        .child(heading(t, this.editingRole.isNew ? "新建角色" : `编辑角色 · ${roleDraft.id}`))
        .child(hint(t, "角色类型"))
        .child(actions().children(["lead", "developer", "reviewer", "specialist"].map(k =>
          button(t, `r-kind-${k}`, roleLabels[k], (_e, cx) => { roleDraft.kind = k; cx.notify(); }, roleDraft.kind === k ? "selected" : "normal"))))
        .child(this.field(t, "名称", this.roleFields.name))
        .child(this.field(t, "职责", this.roleFields.duties, true, 70))
        .child(this.field(t, "交付要求", this.roleFields.deliverables, true, 54))
        .child(this.field(t, "回报约定", this.roleFields.reporting, true, 54))
        .child(actions()
          .child(button(t, "role-cancel", "取消", (_e, cx) => { this.editingRole = null; cx.notify(); }))
          .child(button(t, "role-save", "保存角色", this.busy ? null : (_e, cx) => this.job(cx, async () => {
            const next = JSON.parse(JSON.stringify(config));
            roleDraft.name = this.roleFields.name.value().trim();
            roleDraft.duties = this.roleFields.duties.value().trim();
            roleDraft.deliverables = this.roleFields.deliverables.value().trim();
            roleDraft.reporting = this.roleFields.reporting.value().trim();
            if (!this.editingRole.isNew) roleDraft.revision = (roleDraft.revision || 1) + 1;
            const idx = next.roles.findIndex(item => item.id === roleDraft.id);
            if (idx < 0) next.roles.push(roleDraft); else next.roles[idx] = roleDraft;
            await this.api("save_config", { config: next });
            this.editingRole = null;
            await this.reload();
            this.message = "角色配置已保存。";
          }), "primary"))));
    }
    panel.child(roleGroup);

    const edit = group(t, "高级 JSON 与环境路径");
    if (!this.settingsEditing) edit.child(button(t, "edit-settings", "高级：编辑配置 JSON", (_e, cx) => {
      this.config.set_value(JSON.stringify(config, null, 2)); this.settingsEditing = true; cx.notify();
    }));
    else edit.child(hint(t, "成员 write_allowed 控制申请写入资格。验收 acceptance 设为 chat 或 local。环境路径留空时自动发现。"))
      .child(this.field(t, "配置 JSON", this.config, true, 258))
      .child(actions().child(button(t, "cancel-edit", "取消", this.busy ? null : (_e, cx) => { this.settingsEditing = false; cx.notify(); }))
        .child(button(t, "save-settings", "保存 JSON 设置", this.busy ? null : (_e, cx) => this.job(cx, async () => {
          let next;
          try { next = JSON.parse(this.config.value()); }
          catch { throw Error("团队配置不是有效 JSON。"); }
          await this.api("save_config", { config: next });
          this.settingsEditing = false;
          await this.reload();
          this.message = "团队设置已更新。";
        }), "primary")));
    panel.child(edit);

    const legacy = group(t, "旧团队导入");
    if (!this.preview) legacy.child(button(t, "preview-import", "预览旧团队", this.busy ? null : (_e, cx) => this.job(cx, async () => {
      this.preview = await this.api("preview_import");
    })));
    else {
      const preview = this.preview;
      legacy.child(hint(t, preview.source));
      for (const note of rows(preview.notes)) legacy.child(hint(t, note));
      const previewRoles = new Map(rows(preview.config?.roles).map(role => [role.id, role.name]));
      legacy.child(hint(t, `角色 · ${[...previewRoles.values()].join("、")}`));
      for (const team of rows(preview.config?.teams)) legacy.child(item(t, team.name)
        .child(hint(t, rows(team.members).map(member => [previewRoles.get(member.role) ?? member.id, member.agent, member.model].join(" / ")).join("  ·  "))));
      legacy.child(actions().child(button(t, "cancel-import", "关闭预览", (_e, cx) => { this.preview = null; cx.notify(); }))
        .child(button(t, "apply-import", "确认导入团队设置", this.busy ? null : (_e, cx) => this.job(cx, async () => {
          await this.api("apply_import", { preview_id: preview.preview_id });
          this.preview = null;
          this.settingsEditing = false;
          await this.reload();
          this.message = "已导入角色、团队与模型选择。";
        }), "primary")));
    }
    panel.child(legacy);
    const env = group(t, "本机能力").child(hint(t, this.data?.root));
    if (this.data?.platform?.version) env.child(hint(t, `${this.data.platform.version} · Python ${this.data.platform.python ?? "已连接"} · Core ${this.data.platform.supported ? "接口完整" : "缺少 " + rows(this.data.platform.missing).join(", ")}`));
    if (this.data?.platform?.error) env.child(hint(t, this.data.platform.error));
    for (const [agentName, cap] of Object.entries(agentProfiles)) {
      env.child(hint(t, `${agentName} · ${cap.available ? "可用" : "未发现"} · ${cap.managed_launch ? "支持初始提示启动" : "仅支持绑定已有会话"} · ${cap.sandbox_boundary_verified ? "沙箱与控制目录隔离已核验" : "控制隔离未验证"}${cap.path ? " · " + cap.path : ""}`));
    }
    env.child(hint(t, "插件创建的 Codex / Antigravity 会话在空闲且无菜单/草稿阻塞时自动投递并核验 started 回执；未报告状态或结果待核实的投递可手动核对。"));
    panel.child(env);
    return panel;
  }

  render() {
    const t = theme();
    if (this.role === "button") return h_flex().size_full().items_center().justify_center().bg(t.chrome)
      .child(button(t, "open-coordinator", "团队协调器", () => run("mod.open", { name: MOD })));
    const top = h_flex().items_center().justify_between().gap(10).px(14).py(9).bg(t.chrome)
      .child(text(t, "团队协调器", { bold: true, size: 18 }))
      .child(actions().child(button(t, "refresh", this.busy ? "处理中…" : "刷新", this.busy ? null : (_e, cx) => this.job(cx, () => this.reload())))
        .when(Boolean(this.overlay), el => el.child(button(t, "close", "关闭", () => closeOverlay(this.overlay)))));
    const tabs = actions().px(14).pb(7).border_b(1).border_color(t.border)
      .children(pages.map(([id, title]) => button(t, `tab-${id}`, title, (_e, cx) => { this.page = id; cx.notify(); }, this.page === id ? "selected" : "normal")));
    const main = !this.data ? hint(t, this.busy ? "正在读取协调器运行状态…" : "等待读取本机数据。") :
      this.page === "start" ? this.renderStart(t) : this.page === "settings" ? this.renderSettings(t) : this.renderRuns(t);
    return v_flex().size_full().min_h(0).bg(t.popover).text_color(t.foreground).child(top).child(tabs)
      .when(Boolean(this.error || this.message), el => el.child(text(t, this.error || this.message, { color: this.error ? t.ansi1 : t.muted }).px(14).pt(8)))
      .child(div().id(`content-${this.page}`).flex_1().min_h(0).overflow_y_scrollbar().child(v_flex().p(14).child(main)))
      .child(this.backgroundControls(t));
  }
}
