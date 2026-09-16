"use strict";

const NATIVE_RUNTIME_WINDOW_APP_ID = window.__TAURI__?.core?.invoke
  ? new URLSearchParams(window.location?.search || "").get("runtimeApp")
  : null;

let isGlobalComposing = false;
let lastGlobalCompositionEndTime = 0;
if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("compositionstart", () => {
    isGlobalComposing = true;
  }, { capture: true });
  document.addEventListener("compositionend", () => {
    isGlobalComposing = false;
    lastGlobalCompositionEndTime = Date.now();
  }, { capture: true });
  document.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.keyCode === 13 || event.keyCode === 229 || event.key === "Process") {
      if (event.isComposing || event.keyCode === 229 || event.key === "Process" || isGlobalComposing || (Date.now() - lastGlobalCompositionEndTime < 800)) {
        if (event.target && (event.target.tagName === "INPUT" || event.target.tagName === "TEXTAREA")) {
          event.stopPropagation();
          if (Date.now() - lastGlobalCompositionEndTime < 800 || event.key === "Enter" || event.keyCode === 13) {
            event.preventDefault();
          }
        }
      }
    }
  }, { capture: true });
}

const model = {
  data: null,
  route: "home",
  conversation: [],
  selectedAppId: null,
  storeDetailOpen: false,
  storeFilter: "all",
  storeQuery: "",
  downloadDevice: null,
  runningApp: null,
  runtimeLayoutObserver: null,
  busy: false,
  activeNeedId: null,
  localePreference: "auto",
  locale: null,
  modelSettings: null,
  codeAgentSettings: null,
  networkSettings: null,
  networkStatus: null,
  collaborationStatus: null,
  collaborationBusy: false,
  collaborationError: null,
  composerDraft: "",
  retryTaskId: null,
  retryNeedId: null,
  runtimeDispatching: false,
  runtimeError: null,
  runtimeDraft: {},
  runtimeWindowAppId: NATIVE_RUNTIME_WINDOW_APP_ID,
  runtimeRefreshTimer: null,
  consentExpiryTimer: null,
  runtimeRefreshInFlight: false,
  runtimeRefreshRequest: null,
  runtimeRefreshActive: false,
  desktopHandoffAppId: null,
  storeOpenRequest: null,
  storeOpenPolling: false,
  installWindow: Boolean(window.__TAURI__?.core?.invoke && new URLSearchParams(window.location?.search || "").get("storeInstall") === "1"),
};

const STORAGE_LOCALE_KEY = "vibapp.ui_locale";

  const I18N = {
  "zh-CN": {
    "app_title": "VibApp",
    "app_mode_label": "LOCAL",
    "local_mode": "本机",
    "local_mode_title": "数据与任务只保存在本机",
    "wordmark_home_label": "返回 VibApp 首页",
    "header_nav_label": "主要功能",
    "nav_home": "发现",
    "nav_apps": "应用库",
    "nav_store": "应用商店",
    "download_client": "下载客户端",
    "nav_builds": "开发进度",
    "nav_settings": "设置",
    "status_verified": "已验收",
    "status_quarantined": "隔离预览",
    "status_rejected": "已拒绝",
    "status_succeeded": "已完成",
    "status_running": "进行中",
    "status_failed": "失败",
    "status_pending": "等待中",
    "status_agent_ready": "已交接",
    "status_existing_app_found": "找到现有应用",
    "status_needs_refinement": "需要补充",
    "status_ready_for_builder": "等待 Builder",
    "status_building": "构建中",
    "status_candidate_ready": "候选可用",
    "status_experimental_qa_passed": "私有预览",
    "status_private": "私有",
    "boot_loading": "正在打开 VibApp…",
    "boot_failed": "客户端状态读取失败",
    "composer_send_label": "发送需求",
    "composer_shortcut": "↵ 发送",
    "composer_title_label": "应用名称",
    "composer_input_label": "你想开发什么",
    "composer_name_default": "新应用",
    "composer_consent_label": "允许 AI 分析需求并匹配应用",
    "search_overline": "VIBAPP · LAUNCHER + APPSTORE",
    "search_title": "发现所需，创造所想。",
    "search_lead": "找到合适的应用，或创建自己的。",
    "search_privacy": "生成目标固定为 VibApp Client；宿主系统与窗口尺寸由 Client 自动适配。未明确授权时，不会发送需求或启动开发。",
    "search_placeholder": "描述你想要的应用…",
    "suggestion_timer": "专注计时器",
    "suggestion_files": "每日文件整理",
    "suggestion_inventory": "库存看板",
    "suggestion_timer_full": "做一个有界面的专注计时器，结束后播放本地提醒。",
    "suggestion_files_full": "做一个后台服务，每天早上九点整理我指定目录里的新文件。",
    "suggestion_inventory_full": "做一个库存看板，界面展示库存，后台每十分钟读取一次本地数据。",
    "home_conversation_label": "与 VibApp 智能体的对话",
    "home_head_title": "你的需求",
    "home_new_chat": "新对话",
    "home_thinking": "正在整理需求，再检查 VibApp AppStore 中已有应用…",
    "home_compose_placeholder": "继续补充功能、限制或修改意见…",
    "toast_state_required": "请先明确确认提交这一个 CodeAgent 任务。",
    "toast_external_cost_required": "请单独确认外部 CodeAgent 可能产生费用或消耗配额。",
    "external_cost_confirmation": "我理解所选外部 CodeAgent 可能产生费用或消耗账户配额，并授权仅执行本次任务",
    "toast_missing_schema": "找不到对应的 schema preview，请重新确认 NeedSpec。",
    "queueing_development": "正在排队并启动 CodeAgent",
    "start_runtime": "正在启动独立运行器",
    "development_queued": "所选 CodeAgent 已接单；可在“开发”查看进度",
    "development_queue_failed_prefix": "任务已排队，但 CodeAgent Adapter 未启动：",
    "toast_unknown_error": "未知错误",
    "search_placeholder_error": "请至少用 10 个字符说清楚想要的结果。",
    "page_apps_overline": "MY VIBAPPS",
    "page_apps_empty_title": "你的应用，从这里开始",
    "page_apps_empty_desc": "添加或创建应用后，就能在这里找到它们。",
    "page_apps_empty_action": "发现应用",
    "page_apps_overline_running": "MY VIBAPPS · LAUNCHER",
    "page_apps_title": "应用库",
    "page_apps_desc": "所有应用，一处打开。",
    "page_apps_type": "类型",
    "page_apps_format": "应用格式",
    "page_apps_format_value": "VibApp · Rust Component",
    "page_apps_compat": "兼容方式",
    "page_apps_status": "当前状态",
    "page_apps_service": "后台职责",
    "page_apps_no_service": "无后台服务",
    "page_apps_capabilities": "声明能力",
    "page_apps_open": "打开",
    "page_apps_open_preview": "在 VibApp 内预览",
    "page_apps_unavailable": "当前不可启动",
    "page_apps_daemon_pending": "后台 daemon 尚未接通",
    "page_builds_overline": "VIBAPP DEVELOPMENT",
    "page_builds_title": "开发进度",
    "page_builds_desc": "从想法到应用，跟进每一步。",
    "page_builds_empty": "还没有开发任务。",
    "page_builds_note_waiting": "等待验收信息。",
    "page_runtime_back": "应用",
    "page_runtime_mode_preview": "私有兼容预览",
    "page_runtime_mode_running": "运行中",
    "page_runtime_layout_auto": "自动布局",
    "page_runtime_layout_compact": "紧凑",
    "page_runtime_layout_regular": "标准",
    "page_runtime_layout_wide": "宽屏",
    "page_runtime_note": "Rust Component 只返回语义节点；VibApp Client 负责安全渲染和响应式布局。当前为首屏隔离预览，并非已安装后台实例。",
    "collaboration_title": "与别人一起使用",
    "collaboration_desc": "创建临时共享频道，或输入别人发来的频道 ID。应用拿到的是受控事件，不会获得原始 RTC 或网络权限。",
    "collaboration_local_note": "不共享时，应用数据仍只保存在本机；全零 UUID 永远不会加入网络。",
    "collaboration_confirm": "我确认要为当前应用建立临时网络连接",
    "collaboration_create": "创建共享频道",
    "collaboration_join_placeholder": "粘贴频道 UUID",
    "collaboration_join": "加入频道",
    "collaboration_leave": "退出共享",
    "collaboration_channel": "频道 ID",
    "collaboration_peers": "当前连接 {peers} 个其他节点",
    "collaboration_expires": "临时频道将在 {time} 失效",
    "collaboration_disabled": "请先在设置中启用 P2P 和实时协作。",
    "collaboration_confirm_required": "请先确认本次临时网络连接。",
    "collaboration_created": "共享频道已创建，可以把频道 ID 发给对方。",
    "collaboration_joined": "已加入共享频道。",
    "collaboration_left": "已退出共享频道。",
    "settings_title": "设置",
    "settings_overline": "VIBAPP · 设置",
    "settings_desc": "让 VibApp 更适合你。",
    "settings_section_gui": "通用",
    "settings_gui_locale": "界面语言",
    "settings_gui_locale_desc": "跟随浏览器，也可以手动选择。",
    "settings_gui_locale_hint": "",
    "settings_network": "传输与协作",
    "settings_network_desc": "P2P 文件传输与实时共享",
    "settings_network_enabled": "参与 P2P 网络",
    "settings_network_seed_apps": "为 AppStore 中公开且已验证的应用包提供上传",
    "settings_network_seed_files": "允许用户明确选择的文件参与上传",
    "settings_network_upload": "上传限速（KiB/s，0 为不限速）",
    "settings_network_download": "下载限速（KiB/s，0 为不限速）",
    "settings_network_cache": "本地缓存上限（MiB）",
    "settings_network_transfers": "同时传输任务上限",
    "settings_network_rtc": "启用应用间实时协作（RTC）",
    "settings_network_channels": "同时协作频道上限",
    "settings_network_turn": "启用自定义 TURN 中继",
    "settings_network_turn_urls": "TURN 服务器（每行一个，最多两个）",
    "settings_network_turn_username": "TURN 用户名",
    "settings_network_turn_credential": "TURN 凭据",
    "settings_network_turn_saved": "已保存；留空会保留现有凭据",
    "settings_network_turn_empty": "仅启用 TURN 时需要",
    "settings_network_turn_clear": "清除已保存的 TURN 凭据",
    "settings_network_note": "本地私有数据仍由 VibApp Client 本机存储，不会加入网络。共享频道必须由用户明确创建或加入；应用本身不会获得原始 WebTorrent、RTC 或任意网络权限。",
    "settings_network_save": "保存网络设置",
    "settings_network_saving": "正在保存…",
    "settings_network_saved": "网络设置已安全保存。",
    "settings_network_status": "网络节点状态",
    "settings_network_status_running": "运行中",
    "settings_network_status_stopped": "已停止",
    "settings_network_status_unavailable": "当前不可用",
    "settings_network_status_error": "启动失败",
    "settings_network_status_starting": "正在启动",
    "settings_network_status_unknown": "正在检查",
    "settings_network_foreground": "网页节点仅在本页面打开时参与传输",
    "settings_network_background": "桌面宿主可持续为公开且已验证的应用包提供传输",
    "settings_network_transfer_summary": "活跃传输 {transfers} · 已加入资源 {torrents}",
    "settings_network_start": "启动网络节点",
    "settings_network_stop": "停止网络节点",
    "settings_network_starting": "正在启动…",
    "settings_network_stopping": "正在停止…",
    "settings_network_started": "网络节点已启动。",
    "settings_network_stopped": "网络节点已停止。",
    "settings_network_enable_first": "请先启用“参与 P2P 网络”并保存。",
    "settings_codeagent": "代码代理",
    "settings_codeagent_desc": "选择负责开发应用的智能体",
    "settings_codeagent_provider": "CodeAgent",
    "settings_codeagent_model": "模型（必填）",
    "settings_codeagent_model_hint": "必须明确填写；任务会永久绑定这个模型",
    "settings_codeagent_ready": "本机已安装，可执行",
    "settings_codeagent_missing": "本机未找到可执行程序",
    "settings_codeagent_pending": "已识别；真实执行暂停，等待可靠的进程隔离后端",
    "settings_codeagent_save": "保存 CodeAgent 设置",
    "settings_codeagent_saving": "正在保存…",
    "settings_codeagent_saved": "CodeAgent 设置已保存；下一次确认需求时生效。",
    "settings_codeagent_source_only": "权限边界：CodeAgent 只能产出未信任源码，不能自行编译、验收、安装或上架。",
    "settings_language_auto": "自动",
    "settings_language_zh": "中文（简体）",
    "settings_language_en": "English",
    "settings_byom": "自带模型（BYOM）",
    "settings_byom_desc": "为需求分析与应用匹配选择模型",
    "settings_generation": "通用生成模型",
    "settings_generation_desc": "接待与需求整理等非编程场景共用这个 OpenAI-like 模型。",
    "settings_embedding": "向量模型",
    "settings_embedding_desc": "只用于把需求和应用说明转换成向量，以便先搜索现有应用。",
    "settings_enabled": "启用",
    "settings_base_url": "Base URL",
    "settings_model": "模型名称",
    "settings_protocol": "协议",
    "settings_protocol_chat": "Chat Completions",
    "settings_protocol_responses": "Responses",
    "settings_api_key": "API Key",
    "settings_api_key_saved": "已保存；留空会保留现有密钥",
    "settings_api_key_empty": "可选；密钥不会回显",
    "settings_clear_api_key": "清除已保存的 API Key",
    "settings_timeout": "超时（秒）",
    "settings_max_tokens": "最大输出 Token",
    "settings_temperature": "Temperature",
    "settings_dimensions": "向量维度",
    "settings_save": "保存模型设置",
    "settings_saving": "正在保存…",
    "settings_saved": "模型设置已保存，将用于下一次非 Code Agent 请求。",
    "settings_codeagent_excluded": "Code Agent 不受 BYOM 影响",
    "settings_codeagent_excluded_desc": "Codex、Claude Code、OpenCode 等编程代理继续使用各自独立的账号、适配器和模型配置。",
    "settings_secure_note": "API Key 只保存在本机受限设置文件中，不会回显，也不会通过命令行或环境变量传给模型助手。远程 HTTP 会被拒绝；本机和私有局域网可使用 HTTP。",
    "service_disconnected": "开发服务未连接，暂时无法创建应用。",
    "details_label": "技术详情",
    "service_label": "未连接",
    "settings_help": "使用说明",
    "new_request": "创建应用",
  },
  "en-US": {
    "app_title": "VibApp",
    "app_mode_label": "LOCAL",
    "local_mode": "Local",
    "local_mode_title": "State and tasks are kept only on this device",
    "wordmark_home_label": "Back to VibApp home",
    "header_nav_label": "Main navigation",
    "nav_home": "Discover",
    "nav_apps": "Library",
    "nav_store": "Store",
    "download_client": "Download",
    "nav_builds": "Activity",
    "nav_settings": "Settings",
    "status_verified": "Verified",
    "status_quarantined": "Quarantined",
    "status_rejected": "Rejected",
    "status_succeeded": "Succeeded",
    "status_running": "Running",
    "status_failed": "Failed",
    "status_pending": "Pending",
    "status_agent_ready": "Handed to Agent",
    "status_existing_app_found": "Existing App Found",
    "status_needs_refinement": "Needs Refinement",
    "status_ready_for_builder": "Ready for Builder",
    "status_building": "Building",
    "status_candidate_ready": "Candidate Ready",
    "status_experimental_qa_passed": "Private Preview",
    "status_private": "Private",
    "boot_loading": "Opening VibApp…",
    "boot_failed": "Client state load failed",
    "composer_send_label": "Submit request",
    "composer_shortcut": "↵ Send",
    "composer_title_label": "App name",
    "composer_input_label": "What do you want to build?",
    "composer_name_default": "New app",
    "composer_consent_label": "Allow AI to analyze this request and match apps",
    "search_overline": "VIBAPP · LAUNCHER + APPSTORE",
    "search_title": "Find it. Vibe it.",
    "search_lead": "Find one. Or create your own.",
    "search_privacy": "Generation target is fixed as VibApp Client; host and window size are auto-adjusted by Client. No requirement is sent to remote without explicit consent.",
    "search_placeholder": "Describe the app you have in mind…",
    "suggestion_timer": "Focus timer",
    "suggestion_files": "Daily file sorter",
    "suggestion_inventory": "Inventory dashboard",
    "suggestion_timer_full": "Create a visual focus timer that plays a local alert when time is up.",
    "suggestion_files_full": "Create a background service to organize new files in my folder every morning 9:00.",
    "suggestion_inventory_full": "Create an inventory dashboard that shows inventory and refreshes local data every ten minutes.",
    "home_conversation_label": "Chat with VibApp assistant",
    "home_head_title": "Your request",
    "home_new_chat": "New chat",
    "home_thinking": "organizing your requirement and checking existing VibApp apps in AppStore…",
    "home_compose_placeholder": "Add constraints, requirements, or edits…",
    "toast_state_required": "Please confirm this CodeAgent task before submit.",
    "toast_external_cost_required": "Separately acknowledge that the external CodeAgent may incur charges or consume account quota.",
    "external_cost_confirmation": "I understand the selected external CodeAgent may incur charges or consume account quota, and authorize this task only",
    "toast_missing_schema": "Unable to find schema preview for this request. Please recheck NeedSpec.",
    "queueing_development": "Queueing and starting CodeAgent",
    "start_runtime": "Starting isolated runtime",
    "development_queued": "The selected CodeAgent has taken this task. Check progress in Builds.",
    "development_queue_failed_prefix": "Task queued, but the CodeAgent adapter did not start: ",
    "toast_unknown_error": "Unknown error",
    "search_placeholder_error": "Please describe at least 10 characters.",
    "page_apps_overline": "MY VIBAPPS",
    "page_apps_empty_title": "Make room for your apps.",
    "page_apps_empty_desc": "Apps you add or create will be right here.",
    "page_apps_empty_action": "Discover apps",
    "page_apps_overline_running": "MY VIBAPPS · LAUNCHER",
    "page_apps_title": "Library",
    "page_apps_desc": "All your apps. One place.",
    "page_apps_type": "Type",
    "page_apps_format": "Package format",
    "page_apps_format_value": "VibApp · Rust Component",
    "page_apps_compat": "Compatibility",
    "page_apps_status": "Current status",
    "page_apps_service": "Runtime service",
    "page_apps_no_service": "No service",
    "page_apps_capabilities": "Declared capabilities",
    "page_apps_open": "Open",
    "page_apps_open_preview": "Preview in VibApp",
    "page_apps_unavailable": "Not launchable",
    "page_apps_daemon_pending": "Background daemon not connected",
    "page_builds_overline": "VIBAPP DEVELOPMENT",
    "page_builds_title": "Activity",
    "page_builds_desc": "Follow your ideas as they become apps.",
    "page_builds_empty": "No development tasks yet.",
    "page_builds_note_waiting": "Waiting for acceptance information.",
    "page_runtime_back": "Apps",
    "page_runtime_mode_preview": "Isolated preview",
    "page_runtime_mode_running": "Running",
    "page_runtime_layout_auto": "Auto layout",
    "page_runtime_layout_compact": "Compact",
    "page_runtime_layout_regular": "Regular",
    "page_runtime_layout_wide": "Wide",
    "page_runtime_note": "Rust Component only returns semantic nodes. VibApp Client handles secure rendering and responsive layout. This is a first-screen isolation preview, not an installed background instance.",
    "collaboration_title": "Use together",
    "collaboration_desc": "Create a temporary shared channel, or enter a channel ID from someone else. Apps receive controlled events, never raw RTC or network access.",
    "collaboration_local_note": "Without sharing, app data stays on this device; the all-zero UUID never joins the network.",
    "collaboration_confirm": "I confirm this temporary network connection for the current app",
    "collaboration_create": "Create shared channel",
    "collaboration_join_placeholder": "Paste channel UUID",
    "collaboration_join": "Join channel",
    "collaboration_leave": "Leave sharing",
    "collaboration_channel": "Channel ID",
    "collaboration_peers": "Connected to {peers} other nodes",
    "collaboration_expires": "Temporary channel expires at {time}",
    "collaboration_disabled": "Enable P2P and real-time collaboration in Settings first.",
    "collaboration_confirm_required": "Confirm this temporary network connection first.",
    "collaboration_created": "Shared channel created. You can send its ID to the other person.",
    "collaboration_joined": "Joined the shared channel.",
    "collaboration_left": "Left the shared channel.",
    "settings_title": "Settings",
    "settings_overline": "VIBAPP · SETTINGS",
    "settings_desc": "Make VibApp yours.",
    "settings_section_gui": "General",
    "settings_gui_locale": "Interface language",
    "settings_gui_locale_desc": "Follow your browser, or choose a language.",
    "settings_gui_locale_hint": "",
    "settings_network": "Transfer & collaboration",
    "settings_network_desc": "P2P files and live sharing",
    "settings_network_enabled": "Participate in the P2P network",
    "settings_network_seed_apps": "Upload public verified AppStore packages",
    "settings_network_seed_files": "Allow explicitly selected user files to upload",
    "settings_network_upload": "Upload limit (KiB/s, 0 is unlimited)",
    "settings_network_download": "Download limit (KiB/s, 0 is unlimited)",
    "settings_network_cache": "Local cache limit (MiB)",
    "settings_network_transfers": "Concurrent transfer limit",
    "settings_network_rtc": "Enable real-time app collaboration (RTC)",
    "settings_network_channels": "Concurrent collaboration channel limit",
    "settings_network_turn": "Enable a custom TURN relay",
    "settings_network_turn_urls": "TURN servers (one per line, maximum two)",
    "settings_network_turn_username": "TURN username",
    "settings_network_turn_credential": "TURN credential",
    "settings_network_turn_saved": "Saved; leave blank to retain the credential",
    "settings_network_turn_empty": "Required only when TURN is enabled",
    "settings_network_turn_clear": "Clear the saved TURN credential",
    "settings_network_note": "Local-private data remains in VibApp Client storage and never joins the network. Shared channels require an explicit user create/join action; apps do not receive raw WebTorrent, RTC, or arbitrary network access.",
    "settings_network_save": "Save network settings",
    "settings_network_saving": "Saving…",
    "settings_network_saved": "Network settings saved securely.",
    "settings_network_status": "Network node status",
    "settings_network_status_running": "Running",
    "settings_network_status_stopped": "Stopped",
    "settings_network_status_unavailable": "Unavailable",
    "settings_network_status_error": "Start failed",
    "settings_network_status_starting": "Starting",
    "settings_network_status_unknown": "Checking",
    "settings_network_foreground": "The web node participates only while this page remains open.",
    "settings_network_background": "The desktop host can keep public verified app packages available in the background.",
    "settings_network_transfer_summary": "Active transfers {transfers} · joined resources {torrents}",
    "settings_network_start": "Start network node",
    "settings_network_stop": "Stop network node",
    "settings_network_starting": "Starting…",
    "settings_network_stopping": "Stopping…",
    "settings_network_started": "Network node started.",
    "settings_network_stopped": "Network node stopped.",
    "settings_network_enable_first": "Enable “Participate in the P2P network” and save first.",
    "settings_codeagent": "Code agent",
    "settings_codeagent_desc": "Choose the agent that builds your apps",
    "settings_codeagent_provider": "CodeAgent",
    "settings_codeagent_model": "Model (required)",
    "settings_codeagent_model_hint": "Required; the confirmed task is permanently bound to this exact model",
    "settings_codeagent_ready": "Installed locally and executable",
    "settings_codeagent_missing": "Executable not found locally",
    "settings_codeagent_pending": "Detected; live execution is paused pending a reliable process-containment backend",
    "settings_codeagent_save": "Save CodeAgent settings",
    "settings_codeagent_saving": "Saving…",
    "settings_codeagent_saved": "CodeAgent settings saved and will apply to the next confirmed request.",
    "settings_codeagent_source_only": "Authority boundary: CodeAgent may only produce untrusted source; it cannot compile, verify, install, or publish.",
    "settings_language_auto": "Automatic",
    "settings_language_zh": "Chinese (Simplified)",
    "settings_language_en": "English",
    "settings_byom": "Bring Your Own Model",
    "settings_byom_desc": "Models for understanding requests and finding apps",
    "settings_generation": "General generation model",
    "settings_generation_desc": "Shared by receptionist and requirement-analysis scenarios that do not author code.",
    "settings_embedding": "Embedding model",
    "settings_embedding_desc": "Only converts needs and app descriptions into vectors so existing apps can be searched first.",
    "settings_enabled": "Enabled",
    "settings_base_url": "Base URL",
    "settings_model": "Model name",
    "settings_protocol": "Protocol",
    "settings_protocol_chat": "Chat Completions",
    "settings_protocol_responses": "Responses",
    "settings_api_key": "API Key",
    "settings_api_key_saved": "Saved; leave blank to keep the existing key",
    "settings_api_key_empty": "Optional; secrets are never echoed",
    "settings_clear_api_key": "Clear saved API Key",
    "settings_timeout": "Timeout (seconds)",
    "settings_max_tokens": "Maximum output tokens",
    "settings_temperature": "Temperature",
    "settings_dimensions": "Vector dimensions",
    "settings_save": "Save model settings",
    "settings_saving": "Saving…",
    "settings_saved": "Model settings saved and will apply to the next non-Code-Agent request.",
    "settings_codeagent_excluded": "Code Agent is excluded from BYOM",
    "settings_codeagent_excluded_desc": "Coding agents such as Codex, Claude Code, and OpenCode keep their own accounts, adapters, and model configuration.",
    "settings_secure_note": "API keys stay in a restricted local settings file, are never echoed, and are not passed to helpers through command-line arguments or environment variables. Remote HTTP is rejected; loopback and private-LAN HTTP remain available.",
    "service_disconnected": "Development service not connected. App creation is unavailable.",
    "details_label": "Technical details",
    "service_label": "Not connected",
    "settings_help": "About these settings",
    "new_request": "Create an app",
  }
};

const statusText = {
  verified: { "zh-CN": "已验收", "en-US": "Verified" },
  quarantined: { "zh-CN": "隔离预览", "en-US": "Private Preview" },
  rejected: { "zh-CN": "已拒绝", "en-US": "Rejected" },
  succeeded: { "zh-CN": "已完成", "en-US": "Succeeded" },
  running: { "zh-CN": "进行中", "en-US": "Running" },
  failed: { "zh-CN": "失败", "en-US": "Failed" },
  pending: { "zh-CN": "等待中", "en-US": "Pending" },
  historical: { "zh-CN": "历史记录", "en-US": "Historical record" },
  "legacy-history": { "zh-CN": "历史记录", "en-US": "Historical record" },
  "legacy-state-unproven": { "zh-CN": "旧状态待核对", "en-US": "Historical status unconfirmed" },
  "history-unavailable": { "zh-CN": "记录暂不可读", "en-US": "History unavailable" },
  "agent-ready": { "zh-CN": "已交接", "en-US": "Handed to Agent" },
  "existing-app-found": { "zh-CN": "找到现有应用", "en-US": "Existing App Found" },
  "needs-refinement": { "zh-CN": "需要补充", "en-US": "Needs Refinement" },
  "ready-for-builder": { "zh-CN": "等待 Builder", "en-US": "Ready for Builder" },
  building: { "zh-CN": "构建中", "en-US": "Building" },
  "candidate-ready": { "zh-CN": "候选可用", "en-US": "Candidate Ready" },
  "experimental-qa-passed": { "zh-CN": "私有预览", "en-US": "Experimental QA Passed" },
  private: { "zh-CN": "私有", "en-US": "Private" },
  "public-appstore": { "zh-CN": "AppStore 公开", "en-US": "Public AppStore" },
  "package-verified": { "zh-CN": "安装包已校验", "en-US": "Package checked" },
};

function normalizeLocale(candidate) {
  return /^zh(?:[-_]|$)/i.test(String(candidate || "")) ? "zh-CN" : "en-US";
}

function getDefaultLocale() {
  return normalizeLocale(typeof navigator === "undefined" ? "" : navigator.languages?.[0] || navigator.language);
}

function resolveLocale() {
  const storage = getLocalePreference();
  if (storage === "auto") return getDefaultLocale();
  return normalizeLocale(storage || getDefaultLocale());
}

function getLocalePreference() {
  try {
    const saved = localStorage.getItem(STORAGE_LOCALE_KEY);
    return ["auto", "zh-CN", "en-US"].includes(saved) ? saved : "auto";
  } catch {
    return "auto";
  }
}

function t(key) {
  const locale = model.locale || getDefaultLocale();
  return I18N[locale]?.[key] || I18N["en-US"][key] || key;
}

function lx(zh, en) {
  return model.locale === "en-US" ? en : zh;
}

function codeAgentDisplayName(providerId) {
  const configured = model.codeAgentSettings?.providers?.find(provider => provider.id === providerId);
  if (configured?.displayName) return configured.displayName;
  return ({
    codex: "OpenAI Codex",
    "claude-code": "Claude Code",
    opencode: "OpenCode",
    "gemini-cli": "Gemini CLI",
  })[providerId] || "CodeAgent";
}

function statusLabel(status) {
  return statusText[status]?.[model.locale || "zh-CN"] || status;
}

function updateLocaleUI() {
  document.documentElement.lang = model.locale === "zh-CN" ? "zh-CN" : "en";
  document.title = t("app_title");
  document.querySelectorAll("[data-locale-key]").forEach(node => {
    const web = Boolean(window.VibAppWebBridge?.invoke);
    const key = node.dataset.localeKey;
    node.textContent = web && key === "app_title" ? "VibApp.ai" : t(web && key === "nav_apps" ? "nav_store" : key);
  });
  document.querySelectorAll("[data-locale-title-key]").forEach(node => { node.title = t(node.dataset.localeTitleKey); });
  document.querySelectorAll("[data-locale-aria-key]").forEach(node => { node.setAttribute("aria-label", t(node.dataset.localeAriaKey)); });
  document.querySelectorAll("[data-setting-locale]").forEach(node => { node.value = model.localePreference; });
  document.querySelectorAll("[data-client-download]").forEach(node => { node.hidden = !Boolean(window.VibAppWebBridge?.invoke); });
  const downloadDialog = document.querySelector("#client-download-dialog");
  if (downloadDialog?.open && downloadDialog.dataset.locale !== model.locale) {
    downloadDialog.innerHTML = renderClientDownload();
    downloadDialog.dataset.locale = model.locale;
  }
}

function setLocale(nextLocale) {
  const drafts = captureViewDrafts();
  const disclosures = [...document.querySelectorAll('#view details')].map(node => node.open);
  const normalized = nextLocale === "zh" ? "zh-CN" : nextLocale === "en" ? "en-US" : nextLocale;
  const preference = ["auto", "zh-CN", "en-US"].includes(normalized) ? normalized : "auto";
  model.localePreference = preference;
  try {
    localStorage.setItem(STORAGE_LOCALE_KEY, preference);
  } catch {
    // best effort
  }
  window.VibAppWebBridge?.setLocalePreference?.(preference).catch(() => {
    // The active page still switches immediately; a failed parent storage write
    // will be surfaced by the next Web bridge operation or reload.
  });
  model.locale = preference === "auto" ? getDefaultLocale() : normalizeLocale(preference);
  updateLocaleUI();
  render();
  restoreViewDrafts(drafts);
  document.querySelectorAll('#view details').forEach((node, index) => { node.open = disclosures[index] || false; });
}

// Keep unsent text, unchecked consent and unsaved settings in memory only during
// translation. Never persist secrets or submit a form as a language side effect.
function captureViewDrafts() {
  return [...document.querySelectorAll("#view input, #view textarea, #view select")]
    .filter(node => node.name && node.type !== "hidden")
    .map(node => ({ form: node.form?.id || node.form?.className, name: node.name, type: node.type,
      value: node.value, checked: node.checked, focused: document.activeElement === node,
      start: node.selectionStart, end: node.selectionEnd }));
}
function restoreViewDrafts(drafts) {
  for (const node of document.querySelectorAll("#view input, #view textarea, #view select")) {
    const saved = drafts.find(item => item.form === (node.form?.id || node.form?.className) && item.name === node.name
      && item.type === node.type && (!["checkbox", "radio"].includes(node.type) || item.value === node.value));
    if (!saved) continue;
    if (node.type !== "file") node.value = saved.value;
    if (["checkbox", "radio"].includes(node.type)) node.checked = saved.checked;
    if (saved.focused) { node.focus(); if (typeof saved.start === "number") node.setSelectionRange?.(saved.start, saved.end); }
  }
}

function hostedServiceUnavailable() {
  return document.documentElement.dataset?.vibappHostedShell === "true";
}
function serviceNotice() {
  return hostedServiceUnavailable() ? `<p class="service-notice" role="status">${t("service_disconnected")}</p>` : "";
}

const view = document.querySelector("#view");
const main = document.querySelector("#main");
const toast = document.querySelector("#toast");

function esc(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

// The accepted manifest can integrity-bind generic assets, but it does not yet
// identify an icon role. Until that role exists, the trusted host derives a
// stable, inert visual identity from app_id instead of guessing an asset path.
const appIdentityPalettes = Object.freeze([
  Object.freeze({ base: "#075985", accent: "#38bdf8", ink: "#f0f9ff" }),
  Object.freeze({ base: "#4338ca", accent: "#a78bfa", ink: "#f5f3ff" }),
  Object.freeze({ base: "#9f1239", accent: "#fb7185", ink: "#fff1f2" }),
  Object.freeze({ base: "#9a3412", accent: "#fb923c", ink: "#fff7ed" }),
  Object.freeze({ base: "#166534", accent: "#4ade80", ink: "#f0fdf4" }),
  Object.freeze({ base: "#0f766e", accent: "#2dd4bf", ink: "#f0fdfa" }),
  Object.freeze({ base: "#86198f", accent: "#e879f9", ink: "#fdf4ff" }),
  Object.freeze({ base: "#334155", accent: "#94a3b8", ink: "#f8fafc" }),
]);

const appIdentityMotifs = Object.freeze([
  '<path d="M7 46 27 8h30L37 56H7Z"/>',
  '<circle cx="45" cy="18" r="22"/><circle cx="15" cy="51" r="18"/>',
  '<path d="M-5 18 20-5l49 49-25 25Z"/><path d="m44-4 25 25-12 12L32 8Z"/>',
  '<path d="M8 8h22v22H8zM34 34h22v22H34z"/><circle cx="47" cy="17" r="10"/>',
]);

function hashAppIdentity(value) {
  let hash = 0x811c9dc5;
  for (const character of String(value || "vibapp.unknown")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash >>> 0;
}

function appIdentityModel(app = {}) {
  const appId = String(app.app_id || app.id || "vibapp.unknown");
  const displayName = String(app.display_name || app.displayName || appId || "VibApp");
  const hash = hashAppIdentity(appId);
  const characters = Array.from(displayName.normalize("NFKC").trim());
  return Object.freeze({
    appId,
    displayName,
    monogram: (characters.find(character => /[\p{L}\p{N}]/u.test(character)) || "V").toLocaleUpperCase(),
    palette: appIdentityPalettes[hash % appIdentityPalettes.length],
    paletteIndex: hash % appIdentityPalettes.length,
    motif: appIdentityMotifs[(hash >>> 8) % appIdentityMotifs.length],
    motifIndex: (hash >>> 8) % appIdentityMotifs.length,
  });
}

function appIdentityIcon(app = {}, className = "") {
  const identity = appIdentityModel(app);
  const extraClasses = String(className).split(/\s+/).filter(token => /^[A-Za-z][A-Za-z0-9_-]*$/.test(token));
  return `<span class="app-icon app-icon-generated app-palette-${identity.paletteIndex} ${extraClasses.join(" ")}" data-app-icon-id="${esc(identity.appId)}" data-app-icon-motif="${identity.motifIndex}" aria-hidden="true"><svg viewBox="0 0 64 64" focusable="false" aria-hidden="true">${identity.motif}</svg><span class="app-icon-monogram">${esc(identity.monogram)}</span></span>`;
}

function publicationBadge(app = {}) {
  const state = app.publication_badge
    || (app.publication_state === "published" ? "public-appstore" : app.publication_state)
    || "private";
  return badge(state);
}

function icon(name) {
  const paths = {
    send: '<path d="m5 12 14-7-5.5 14-2.4-5.6z"/><path d="m11.1 13.4 3.4-3.4"/>',
    spark: '<path d="M12 3l1.3 4.2L17 9l-3.7 1.8L12 15l-1.3-4.2L7 9l3.7-1.8z"/><path d="M18.5 15l.7 2.2L21 18l-1.8.8-.7 2.2-.7-2.2L16 18l1.8-.8z"/>',
    back: '<path d="m15 18-6-6 6-6"/>',
    play: '<path d="m9 7 8 5-8 5z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    clock: '<circle cx="12" cy="12" r="8"/><path d="M12 8v5l3 2"/>',
    arrow: '<path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M7.5 8.5A6 6 0 0 1 18 12"/><path d="M16.5 15.5A6 6 0 0 1 6 12"/>',
    box: '<path d="m4 7 8-4 8 4-8 4z"/><path d="M4 7v10l8 4 8-4V7M12 11v10"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    download: '<path d="M12 3v12m-4-4 4 4 4-4M5 15v4a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-4"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m16 16 5 5"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18Z"/>',
    monitor: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M12 17v4m-4 0h8"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || ""}</svg>`;
}

function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(statusLabel(status))}</span>`;
}

function dateLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(model.locale === "en-US" ? "en-US" : "zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function showToast(message, error = false) {
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { toast.className = "toast"; }, 3800);
}

async function call(command, payload = {}) {
  if (window.__TAURI__?.core?.invoke) {
    return window.__TAURI__.core.invoke(command, payload);
  }
  if (window.VibAppWebBridge?.invoke) {
    return window.VibAppWebBridge.invoke(command, payload);
  }
  const routes = {
    get_state: ["/api/state", "GET"],
    submit_need: ["/api/needs", "POST"],
    complete_need: ["/api/needs/complete", "POST"],
    get_model_settings: ["/api/settings/models", "GET"],
    save_model_settings: ["/api/settings/models", "POST"],
    get_codeagent_settings: ["/api/settings/codeagents", "GET"],
    save_codeagent_settings: ["/api/settings/codeagents", "POST"],
    get_network_settings: ["/api/settings/network", "GET"],
    save_network_settings: ["/api/settings/network", "POST"],
    get_network_status: ["/api/settings/network/status", "GET"],
    start_network_node: ["/api/settings/network/start", "POST"],
    stop_network_node: ["/api/settings/network/stop", "POST"],
    submit_development_task: ["/api/development-tasks", "POST"],
    submit_install_intent: ["/api/install-intents", "POST"],
  };
  if (!routes[command]) throw new Error(lx("此操作需要在 VibApp 原生客户端中运行。", "This action requires the native VibApp client."));
  const [path, method] = routes[command];
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: method === "POST" ? JSON.stringify(payload.payload) : undefined,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error?.message || lx(`请求失败（${response.status}）`, `Request failed (${response.status})`));
  return body;
}

function renderSearchHome() {
  return `<div class="search-home">
    <div class="search-intro">
      <h1>${t("search_title")}</h1>
      <p class="lead">${t("search_lead")}</p>
    </div>
    ${composer("hero-composer", t("search_placeholder"))}
    <div class="suggestions" aria-label="${t("home_conversation_label")}">
      <button data-suggestion="${t("suggestion_timer_full")}">${t("suggestion_timer")}</button>
      <button data-suggestion="${t("suggestion_files_full")}">${t("suggestion_files")}</button>
      <button data-suggestion="${t("suggestion_inventory_full")}">${t("suggestion_inventory")}</button>
    </div>
    ${serviceNotice()}
  </div>`;
}

function composer(id, placeholder) {
  return `<form id="${id}" class="composer" aria-label="${lx("描述应用需求", "Describe the app request")}">
    <label class="sr-only" for="${id}-title">${t("composer_title_label")}</label>
    <input id="${id}-title" name="title" maxlength="80" value="${t("composer_name_default")}" hidden>
    <label class="sr-only" for="${id}-input">${t("composer_input_label")}</label>
    <textarea id="${id}-input" name="description" rows="1" maxlength="2000" placeholder="${esc(placeholder)}" required>${esc(model.composerDraft || "")}</textarea>
    <button class="send-button" type="submit" aria-label="${t("composer_send_label")}" ${model.busy || hostedServiceUnavailable() ? "disabled" : ""}>${model.busy ? '<span class="button-spinner"></span>' : icon("send")}</button>
    <div class="composer-meta"><label class="consent-toggle"><input name="embedding_consent" type="checkbox" value="granted" checked><span>${t("composer_consent_label")}</span></label><span>${t("composer_shortcut")}</span></div>
    <p class="composer-error" role="alert"></p>
  </form>`;
}

function renderConversation() {
  return `<div class="conversation-shell">
    <section class="conversation" aria-label="${t("home_conversation_label")}">
      <div class="conversation-head">
        <div><strong>${t("app_title")}</strong><small>${t("home_head_title")}</small></div>
        <button class="quiet-button" data-new-chat>${t("home_new_chat")}</button>
      </div>
      <div class="messages">
        ${model.conversation.map(message => renderMessage(message)).join("")}
        ${model.busy ? `<article class="message assistant-message"><div class="message-avatar">${icon("spark")}</div><div class="message-body"><div class="thinking"><span></span><span></span><span></span></div><p>${t("home_thinking")}</p></div></article>` : ""}
      </div>
    </section>
    <div class="conversation-composer">${composer("chat-composer", t("home_compose_placeholder"))}</div>
  </div>`;
}

function renderMessage(message) {
  if (message.role === "user") {
    return `<article class="message user-message" data-need-id="${esc(message.needId || "")}" data-task-id="${esc(message.taskId || "")}" data-attempt-id="${esc(message.attemptId || "")}"><div class="message-body"><p>${esc(message.text)}</p></div></article>`;
  }
  if (message.error) {
    return `<article class="message assistant-message" data-need-id="${esc(message.needId || "")}" data-task-id="${esc(message.taskId || "")}" data-attempt-id="${esc(message.attemptId || "")}"><div class="message-avatar">${icon("spark")}</div><div class="message-body error-card"><strong>${lx("这次操作没有完成", "This operation did not complete")}</strong><p>${esc(message.text)}</p></div></article>`;
  }
  if (message.job) return renderJobConversation(message.job, message.need);
  if (message.text && !message.result) {
    return `<article class="message assistant-message" data-need-id="${esc(message.needId || "")}" data-task-id="${esc(message.taskId || "")}" data-attempt-id="${esc(message.attemptId || "")}"><div class="message-avatar">${icon("spark")}</div><div class="message-body"><p>${esc(message.text)}</p></div></article>`;
  }
  const result = message.result || {};
  const registry = result.registry || {};
  const needSpec = result.need_spec || {};
  const consent = result.consent || {};
  const cloud = result.cloud_development || {};
  const analysis = needSpec.analysis || {};
  const stateCard = `<div class="route-state-card">
    <div><span>NeedSpec</span><strong>${needSpec.state === "complete" ? lx("已确认", "Confirmed") : lx("草稿，尚未确认", "Draft, not confirmed")}</strong></div>
    <div><span>${lx("LLM 预处理", "LLM preprocessing")}</span><strong>${analysis.status === "analyzed" ? lx("AI 已整理", "Refined by AI") : analysis.status === "degraded" ? lx("已退回本地规则", "Using local fallback") : consent.lan_llm_analysis === "granted" ? lx("本次已授权", "Authorized for this request") : lx("未授权调用", "Not authorized")}</strong></div>
    <div><span>${lx("语义检索授权", "Semantic retrieval consent")}</span><strong>${consent.registry_embedding === "granted" ? lx("已授权本次局域网匹配", "LAN matching authorized once") : lx("未授权", "Not authorized")}</strong></div>
    <div><span>${lx("云端开发", "Remote development")}</span><strong>${cloud.enabled ? lx("可用", "Available") : lx("禁用", "Disabled")}</strong></div>
  </div>`;
  const blockers = (cloud.blockers || []).map(item => `<code>${esc(item)}</code>`).join("");
  if (needSpec.state === "complete") {
    return renderCompletedNeed(result, stateCard, blockers);
  }
  const refinementForm = model.activeNeedId === result.need?.need_id ? renderNeedSpecForm(result) : "";
  if (registry.route === "recommendation") {
    const recommendations = registry.recommendations || [];
    return `<article class="message assistant-message">
      <div class="message-avatar">${icon("spark")}</div>
      <div class="message-body">
        <p>${lx("我先检查了 VibApp AppStore，找到了符合当前 Client 能力、权限上限与需求描述的现有应用。", "I checked VibApp AppStore first and found existing apps compatible with the current Client, permission ceiling, and request.")}</p>
        <div class="recommendation-list">${recommendations.map(renderRecommendation).join("")}</div>
        ${stateCard}
        <div class="handoff-line">${icon("check")}<div><strong>${lx("优先推荐现有应用", "Existing app recommended first")}</strong><small>${lx("没有创建 CodeAgent 任务，也没有启动代码生成或编译。", "No CodeAgent task, code generation, or build was started.")}</small></div></div>
        <p class="truth-note">${lx("云 worker 合同可用但没有被调用；当前阻止项：", "The remote worker contract is available but was not called; current blockers: ")}${blockers}${lx("。推荐仅来自 Registry 元数据，不代表已安装。", ". Recommendations come from Registry metadata and are not installed.")}</p>
        ${refinementForm}
      </div>
    </article>`;
  }
  const refinement = registry.refinement || {};
  const questions = result.refinement_questions || [];
  const unsupportedCombination = refinement.reason_code === "unsupported-capability-combination";
  const refinementMessage = unsupportedCombination
    ? lx("当前 VibApp 平台不支持所选应用形态与能力组合。", "The current VibApp platform does not support the selected app kind and capability combination.")
    : (refinement.message || lx("当前没有可接受的匹配。", "No acceptable match is available."));

  if (unsupportedCombination) {
    return `<article class="message assistant-message">
      <div class="message-avatar">${icon("spark")}</div>
      <div class="message-body">
        <p>${lx("能力兼容检查已经给出确定结果：这不是“需求还需补充”。", "The capability compatibility check reached a definite result; this is not a request that merely needs more detail.")}</p>
        <div class="refinement-card compatibility-blocker"><strong>${esc(refinement.reason_code || "needs-refinement")}</strong><p>${esc(refinementMessage)}</p></div>
        ${questions.length ? `<section class="missing-prompt compatibility-details"><strong>${lx("兼容性详情", "Compatibility details")}</strong><ol>${questions.map(question => `<li>${esc(question)}</li>`).join("")}</ol></section>` : ""}
        ${stateCard}
        <div class="handoff-line">${icon("check")}<div><strong>${lx("已在能力兼容检查阶段安全停止", "Safely stopped at capability compatibility checking")}</strong><small>${lx("未进入本地开发队列；未调用云 CodeAgent、生成代码或启动编译。", "Nothing entered the local build queue; no remote CodeAgent, generation, or build was started.")}</small></div></div>
        <p class="truth-note">${lx("可以调整应用形态、capabilities 或网络要求；只补充需求文字不会解除此限制。阻止项：", "Change the app kind, capabilities, or network requirement; adding request prose alone will not remove this limit. Blockers: ")}${blockers}.</p>
        ${refinementForm}
      </div>
    </article>`;
  }

  return `<article class="message assistant-message">
    <div class="message-avatar">${icon("spark")}</div>
    <div class="message-body">
      <p class="assistant-lead">${lx("已根据您的输入梳理需求草稿，只需补充或确认必要信息即可开始创建应用：", "Drafted requirements based on your request. Just review or complete the essentials below to start building:")}</p>
      ${questions.length ? `<ol class="refinement-questions">${questions.map(question => `<li>${esc(question)}</li>`).join("")}</ol>` : ""}
      <details class="technical-details">
        <summary>${lx("技术审核与内部状态", "Technical audit & internal state")}</summary>
        <div class="refinement-card"><strong>${esc(refinement.reason_code || "needs-refinement")}</strong><p>${esc(refinementMessage)}</p></div>
        ${stateCard}
        <div class="handoff-line pending">${icon("clock")}<div><strong>${lx("仍在需求细化阶段", "Request refinement is still in progress")}</strong><small>${lx("未进入本地开发队列；未调用云 CodeAgent、生成代码或启动编译。", "Nothing entered the local build queue; no remote CodeAgent, generation, or build was started.")}</small></div></div>
        <p class="truth-note">${lx("推断形态：", "Inferred kind: ")}${esc(kindLabel(needSpec.inferred_app_kind))}${lx("。云 worker 合同可用但没有被调用；阻止项：", ". The remote worker contract is available but was not called; blockers: ")}${blockers}.</p>
      </details>
      ${refinementForm}
    </div>
  </article>`;
}

function safeModelRequestBudget(value, requests) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 11
    || value.schema_version !== "vibapp.model-request-budget-v1" || !["authoring", "repair"].includes(value.phase)) return null;
  for (const phase of ["total", "authoring", "repair"]) {
    for (const field of ["limit", "used", "remaining"]) {
      const number = value[`${phase}_${field}`];
      if (!Number.isInteger(number) || number < 0 || number > 64) return null;
    }
    if (value[`${phase}_limit`] < 1 || value[`${phase}_used`] + value[`${phase}_remaining`] !== value[`${phase}_limit`]) return null;
  }
  return value.total_limit === value.authoring_limit + value.repair_limit
    && value.total_used === value.authoring_used + value.repair_used && value.total_used === requests
    && (value.phase !== "authoring" || value.repair_used === 0) ? value : null;
}

function safeFailureDiagnostic(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 14
    || value.schema_version !== "vibapp.docker-failure-diagnostic-v1"
    || !["provider-process", "provider-spawn", "provider-output-limit", "bridge-process", "host-relay", "host-control", "host-deadline", "host-cancellation", "host-protocol", "unknown"].includes(value.failure_origin)
    || !["stream-decode", "stream-disconnected", "authentication", "rate-limit", "thread-resource", "process-failed", "unknown", "none"].includes(value.provider_error_category)
    || (value.child_signal !== null && !["SIGABRT", "SIGBUS", "SIGFPE", "SIGHUP", "SIGILL", "SIGINT", "SIGKILL", "SIGPIPE", "SIGQUIT", "SIGSEGV", "SIGTERM", "SIGTRAP", "SIGXCPU", "SIGXFSZ", "other"].includes(value.child_signal))) return null;
  for (const [key, maximum] of [["child_exit_code", 255], ["container_exit_code", 255], ["stdout_bytes", 8388608], ["stderr_bytes", 8388608], ["bridge_stdout_bytes", 8388608], ["bridge_stderr_bytes", 8388608]]) {
    if (value[key] !== null && (!Number.isInteger(value[key]) || value[key] < 0 || value[key] > maximum)) return null;
  }
  for (const key of ["output_limit_exceeded", "frame_limit_exceeded", "container_oom_killed", "container_running"]) {
    if (value[key] !== null && typeof value[key] !== "boolean") return null;
  }
  return value;
}

function renderCodeagentDiagnostics(job) {
  const diagnostic = job.codeagent_diagnostics;
  if (!diagnostic || typeof diagnostic !== "object" || Array.isArray(diagnostic)) return "";
  const count = (value, maximum) => Number.isInteger(value) && value >= 0 && value <= maximum;
  const state = diagnostic.state;
  if (!["authoring", "compiler-feedback", "model-retry", "succeeded", "failed", "cancelled"].includes(state)
    || !count(diagnostic.model_requests, 64) || !count(diagnostic.compiler_checks, 3)) return "";
  const finished = ["succeeded", "failed", "private-appstore-ready", "delivery-worker-failed", "builder-not-configured"].includes(job.status)
    || ["verified", "failed"].includes(job.verification?.status);
  if (finished && ["authoring", "compiler-feedback", "model-retry"].includes(state)) return "";
  if ((job.status === "succeeded" || job.status === "private-appstore-ready" || job.verification?.status === "verified") && state !== "succeeded") return "";
  const budget = safeModelRequestBudget(diagnostic.model_request_budget, diagnostic.model_requests);
  if (diagnostic.model_request_budget !== undefined && !budget) return "";
  const terminal = ["succeeded", "failed", "cancelled"].includes(state);
  for (const [key, maximum] of [["logical_model_requests", 64], ["model_retries", 8]]) {
    if (diagnostic[key] !== undefined && (!count(diagnostic[key], maximum)
      || diagnostic[key] > diagnostic.model_requests + Number(terminal && key === "logical_model_requests"))) return "";
  }
  if (diagnostic.logical_model_requests !== undefined && diagnostic.model_retries !== undefined) {
    const attempts = diagnostic.logical_model_requests + diagnostic.model_retries;
    if (attempts < diagnostic.model_requests || attempts > diagnostic.model_requests + Number(terminal)) return "";
  }
  const metrics = [lx(`模型网络请求 ${diagnostic.model_requests} 次`, `Model network attempts: ${diagnostic.model_requests}`)];
  if (count(diagnostic.logical_model_requests, 64)) metrics.push(lx(`逻辑请求 ${diagnostic.logical_model_requests} 次`, `Logical requests: ${diagnostic.logical_model_requests}`));
  if (count(diagnostic.model_retries, 8)) metrics.push(lx(`已执行重试 ${diagnostic.model_retries} 次`, `Retries performed: ${diagnostic.model_retries}`));
  metrics.push(lx(`编译检查 ${diagnostic.compiler_checks} 次`, `Compiler checks: ${diagnostic.compiler_checks}`));
  let message = "";
  let budgetMessage = "";
  if (budget) {
    metrics[0] = lx(`模型网络请求 ${budget.total_used}/${budget.total_limit} 次`, `Model network attempts: ${budget.total_used}/${budget.total_limit}`);
    const remaining = budget[`${budget.phase}_remaining`];
    budgetMessage = `<p>${esc(budget.phase === "authoring"
      ? lx(`初次编写剩余 ${remaining} 次；另预留 ${budget.repair_remaining} 次用于编译后的修复。`, `Initial authoring: ${remaining} attempts remaining; ${budget.repair_remaining} reserved for compiler repairs.`)
      : lx(`编译修复剩余 ${remaining} 次（多轮修复共用）。`, `Compiler repairs: ${remaining} attempts remaining, shared across repair rounds.`))} ${esc(lx("网络重试也计入额度；这不是完成百分比。", "Network retries also use this allowance; it is not a completion percentage."))}</p>`;
  }
  if (state === "model-retry") {
    const retry = diagnostic.retry;
    if (!retry || typeof retry !== "object" || Array.isArray(retry)
      || Object.keys(retry).length !== 5 || !count(retry.attempt, 3) || retry.attempt < 2 || retry.max_attempts !== 3
      || ![429, 502, 503, 504].includes(retry.http_status) || typeof retry.delay_seconds !== "number"
      || !Number.isFinite(retry.delay_seconds) || retry.delay_seconds < 0 || retry.delay_seconds > 30
      || retry.reason !== (retry.http_status === 429 ? "provider-rate-limited" : "provider-upstream-unavailable")
      || !count(diagnostic.logical_model_requests, budget?.total_limit ?? 24) || !count(diagnostic.model_retries, 8)
      || diagnostic.model_requests > (budget?.total_limit ?? 24) || diagnostic.logical_model_requests + diagnostic.model_retries !== diagnostic.model_requests) return "";
    const delay = Math.ceil(retry.delay_seconds);
    message = `<p role="status">${esc(lx(`服务暂时${retry.http_status === 429 ? "限流" : "不可用"}（HTTP ${retry.http_status}），正在等待重试。将在约 ${delay} 秒后进行本次请求的第 ${retry.attempt}/${retry.max_attempts} 次尝试，保留当前编写会话。`, `The model service is temporarily ${retry.http_status === 429 ? "rate-limited" : "unavailable"} (HTTP ${retry.http_status}). Waiting about ${delay}s before attempt ${retry.attempt}/${retry.max_attempts} of this request, in the same authoring session.`))}</p>`;
  } else if (["failed", "cancelled"].includes(state)) {
    const guidance = {
      "provider-upstream-unavailable": ["模型上游暂时不可用，本次自动恢复已停止。请稍后检查服务，再确认新的开发尝试；不会无限重试。", "The model upstream is temporarily unavailable and automatic recovery has stopped. Check the service later, then explicitly confirm a new development attempt; retries are bounded."],
      "provider-rate-limited": ["模型服务限流，本次自动恢复已停止。请等待配额恢复，再确认新的开发尝试。", "The model service is rate-limited and automatic recovery has stopped. Wait for capacity, then explicitly confirm a new development attempt."],
      "provider-authentication-failed": ["模型认证失败，未自动重试。请在设置中检查账户或凭据，再重新确认开发。", "Model authentication failed; it was not automatically retried. Check the account or credentials in Settings before confirming a new attempt."],
      "provider-upstream-rejected": ["模型服务返回错误，本次开发已停止。请检查 HTTP 状态、服务可用性和模型或请求配置，再决定是否开始新的尝试。", "The model service returned an error and this development attempt stopped. Review the HTTP status, service availability, and model or request configuration before deciding whether to start a new attempt."],
      "provider-timeout": ["模型请求超时。为避免重放可能已交付的响应，未自动重放；请检查服务后再确认新的尝试。", "The model request timed out. No uncertain response was automatically replayed; check the service before confirming a new attempt."],
      "provider-network-error": ["模型连接中断。为避免重复处理，未自动重放不确定的请求；请检查网络与服务。", "The model connection failed. An uncertain request was not automatically replayed; check the network and service."],
      "provider-cancelled": ["开发已取消，自动恢复已停止。需要继续时请明确提交新的尝试。", "Development was cancelled and automatic recovery stopped. Explicitly submit a new attempt if you want to continue."],
      "provider-request-budget-exhausted": ["本次开发已达到模型请求预算，所有重试都计入预算。请查看记录后再决定是否提交新的尝试。", "This development attempt reached its model request budget, including retries. Review the history before deciding whether to submit a new attempt."],
      "provider-request-limit": ["模型请求超出限制。请检查任务大小和模型配置。", "The model request exceeded a limit. Review task size and model configuration."],
      "provider-response-limit": ["模型响应超出大小限制，已安全停止。请缩小任务后重新确认。", "The model response exceeded its size limit and stopped safely. Reduce the task size before confirming again."],
      "docker-cleanup-unconfirmed": ["无法确认隔离工作区已清理。请先检查工作进程，暂勿重复提交。", "Isolated worker cleanup could not be confirmed. Check the worker before submitting again."],
      "provider-failed": ["编程智能体退出，本次没有完成交付。请查看下方诊断；未记录到的原因不会被猜测为网络或认证问题。", "The coding agent exited without completing delivery. Review the diagnostics below; missing evidence does not establish a network or authentication failure."],
      "provider-container-failed": ["编程工作进程退出，本次交付未完成。请检查运行环境后再确认新的尝试。", "The coding worker exited without completing delivery. Check its runtime environment before confirming a new attempt."],
    };
    const hint = typeof diagnostic.failure_code === "string" && Object.hasOwn(guidance, diagnostic.failure_code) ? guidance[diagnostic.failure_code] : null;
    if (hint) {
      const http = count(diagnostic.upstream_status, 599) && diagnostic.upstream_status >= 400
        && ["provider-upstream-unavailable", "provider-rate-limited", "provider-authentication-failed", "provider-upstream-rejected"].includes(diagnostic.failure_code) ? ` · HTTP ${diagnostic.upstream_status}` : "";
      message = `<p role="status"><strong>${esc(diagnostic.failure_code)}${http}</strong><br>${esc(lx(...hint))}</p>`;
    }
    const observation = safeFailureDiagnostic(diagnostic.failure_diagnostic);
    const causes = {
      "stream-decode": ["智能体报告：无法解析模型响应流。请检查模型服务的流式协议兼容性。", "Agent-reported: the model response stream could not be decoded. Check streaming protocol compatibility."],
      "stream-disconnected": ["智能体报告：模型响应流中断。请检查模型服务与连接。", "Agent-reported: the model response stream disconnected. Check the service and connection."],
      "authentication": ["智能体报告：认证被拒绝。请检查设置中的模型账户。", "Agent-reported: authentication was rejected. Check the model account in Settings."],
      "rate-limit": ["智能体报告：模型服务限流。请等待配额恢复。", "Agent-reported: the model service is rate-limited. Wait for capacity."],
      "thread-resource": ["智能体报告：无法创建工作线程。请检查工作进程资源限制。", "Agent-reported: a worker thread could not be created. Check worker resource limits."],
    };
    if (observation) {
      if (observation.container_oom_killed === true) message += `<p>${esc(lx("运行环境确认：容器因内存不足被终止。需要检查任务和资源限制，不能直接重复提交。", "Runtime confirmed: the container was killed for exceeding memory capacity. Review the task and resource limits before resubmitting."))}</p>`;
      else if (Object.hasOwn(causes, observation.provider_error_category)) message += `<p>${esc(lx(...causes[observation.provider_error_category]))}</p>`;
      else if (["provider-failed", "provider-container-failed"].includes(diagnostic.failure_code)) message += `<p>${esc(lx("已记录退出信息，但目前仍不能确定根因。", "Exit observations were recorded, but the root cause is still undetermined."))}</p>`;
      const facts = [];
      if (observation.child_exit_code !== null) facts.push(lx(`智能体退出码 ${observation.child_exit_code}`, `Agent exit code: ${observation.child_exit_code}`));
      if (observation.child_signal !== null) facts.push(lx(`退出信号 ${observation.child_signal}`, `Exit signal: ${observation.child_signal}`));
      if (observation.container_exit_code !== null) facts.push(lx(`容器退出码 ${observation.container_exit_code}`, `Container exit code: ${observation.container_exit_code}`));
      if (facts.length) message += `<details><summary>${lx("退出详情", "Exit details")}</summary><small>${esc(facts.join(" · "))}</small></details>`;
    } else if (diagnostic.failure_code === "provider-failed") message += `<p>${esc(lx("这条记录没有可用的退出诊断，根因未知。", "This record has no usable exit diagnostics; the root cause is unknown."))}</p>`;
  }
  return `<section class="job-note codeagent-diagnostics" aria-label="${lx("模型执行状态", "Model execution status")}">${message}${budgetMessage}<small>${esc(metrics.join(" · "))}</small></section>`;
}

function renderJobConversation(job, need) {
  const error = job.error || job.diagnostic || job.codeagent_adapter_status?.error;
  const history = [job.history, job.history?.attempts, job.attempts].find(Array.isArray) || [];
  const attempts = history.filter(attempt => attempt && typeof attempt === "object" && !Array.isArray(attempt));
  const outputs = job.outputs || {};
  const appId = outputs.app_id || outputs.appstore_app_id || job.app_id;
  const app = (model.data?.apps || []).find(item => item.app_id === appId);
  const jobPackageDigest = outputs.package_digest_sha256;
  const exactJobApp = app
    && /^[0-9a-f]{64}$/.test(jobPackageDigest || "")
    && app.package_digest_sha256 === jobPackageDigest;
  const productAssessment = job.task_kind === "product-assessment";
  const failed = !productAssessment && (job.status === "failed" || job.verification?.status === "failed");
  const assessmentReason = job.assessment?.reason_code || attempts.at(-1)?.reason_code;
  return `<article class="message assistant-message" data-need-id="${esc(job.need_id || need?.need_id || "")}" data-task-id="${esc(job.task_id || job.job_id || "")}" data-attempt-id="${esc(job.attempt_id || "")}">
    <div class="message-avatar">${icon("spark")}</div>
    <div class="message-body">
      <p>${productAssessment
        ? lx("这是一次已经完成的产品能力评估，不是 CodeAgent 开发任务。", "This is a completed product capability assessment, not a CodeAgent development task.")
        : failed
          ? (model.locale === "en-US" ? "This development attempt stopped safely. The failure is preserved below." : "这次开发已安全停止，失败原因和旧记录都保留在下面。")
          : (model.locale === "en-US" ? "This is the durable development history for your request." : "这是该需求对应的持久化开发记录。")}</p>
      <section class="complete-card"><span>${esc(job.current_stage || job.stage || "development")}</span><div><strong>${esc(job.title || need?.title || "VibApp")}</strong><small>${esc(job.task_id || job.job_id || "")}</small></div><p>${esc(job.verification?.summary || job.summary || t("page_builds_note_waiting"))}</p></section>
      ${renderCodeagentDiagnostics(job)}
      ${productAssessment ? `<div class="refinement-card compatibility-blocker"><strong>${esc(assessmentReason || "unsupported-capability-combination")}</strong><p>${lx("评估记录绑定了当时的 NeedSpec 摘要与需求摘要；CodeAgent task_created=false，external_request_made=false。", "The record is bound to the NeedSpec and request summary; CodeAgent task_created=false and external_request_made=false.")}</p></div>` : ""}
      ${error ? `<div class="error-card"><strong>${esc(error.code || "development-failed")}</strong><p>${esc(error.message || String(error))}</p><small>${esc(error.stage || job.stage || "")}</small></div>` : ""}
      ${attempts.length ? `<details class="leaving-summary" open><summary>${productAssessment ? lx("评估记录", "Assessment record") : (model.locale === "en-US" ? "Attempts" : "开发尝试记录")}</summary>${attempts.map(attempt => {
        const attemptError = attempt.error;
        const structuredError = attemptError && typeof attemptError === "object" && !Array.isArray(attemptError);
        const errorCode = structuredError && typeof attemptError.code === "string" ? attemptError.code : "failed";
        const errorMessage = structuredError && typeof attemptError.message === "string" ? attemptError.message : (typeof attemptError === "string" ? attemptError : "");
        return `<p><strong>${esc(attempt.attempt_id || attempt.job_id || "attempt")}</strong> · ${esc(attempt.status)} · ${esc(attempt.stage || "")}${attemptError ? `<br><small>${esc(errorCode || "failed")} · ${esc(errorMessage)}</small>` : ""}</p>`;
      }).join("")}</details>` : ""}
      <div class="app-actions">
        <button class="secondary-button" data-route="builds">${model.locale === "en-US" ? "Back to builds" : "返回开发列表"}</button>
        ${failed ? `<button class="primary-button" data-edit-job="${esc(job.task_id || job.job_id || "")}" data-edit-need="${esc(job.need_id || need?.need_id || "")}">${model.locale === "en-US" ? "Edit and retry" : "修改需求后重试"}</button>` : ""}
        ${app ? `<button class="secondary-button" data-job-app="${esc(app.app_id)}">${model.locale === "en-US" ? "App details" : "应用详情"}</button>${app.launch_eligible && exactJobApp ? `<button class="primary-button" data-job-run="${esc(app.app_id)}" data-job-package-digest="${esc(jobPackageDigest)}">${icon("play")}${model.locale === "en-US" ? "Run this build" : "运行此构建"}</button>` : ""}` : ""}
      </div>
    </div>
  </article>`;
}

function isTranscriptEntry(entry, needId) {
  return Boolean(entry && entry.schema_version === "vibapp.conversation-entry.experimental-v1"
    && entry.need_id === needId
    && ["user", "assistant", "system"].includes(entry.role)
    && Number.isSafeInteger(entry.sequence)
    && typeof entry.entry_id === "string"
    && typeof entry.kind === "string"
    && typeof entry.content === "string");
}

function conversationForJob(job) {
  const needId = job.need_id;
  const records = (model.data?.needs || []).filter(item => item?.need_id === needId);
  const seen = new Set();
  const entries = records
    .flatMap(record => Array.isArray(record.conversation_transcript) ? record.conversation_transcript : [])
    .filter(entry => isTranscriptEntry(entry, needId) && !seen.has(entry.entry_id) && seen.add(entry.entry_id))
    .sort((left, right) => left.sequence - right.sequence);
  const messages = entries.map(entry => {
    const identity = { needId: entry.need_id, taskId: entry.task_id, attemptId: entry.attempt_id, kind: entry.kind };
    if (entry.role === "user") return { role: "user", text: entry.content, ...identity };
    if (entry.snapshot && typeof entry.snapshot === "object") return { role: "assistant", result: entry.snapshot, ...identity };
    return { role: "assistant", text: entry.content, ...identity };
  });
  const latestNeed = records.at(-1) || null;
  messages.push({ role: "assistant", job, need: latestNeed, kind: "task-history" });
  return messages;
}

const worldPresets = {
  ui: {
    world: "ui-only-reference",
    capabilities: ["kv", "settings"],
    permissions: ["clock", "kv", "log", "host-info", "settings"],
    network: "offline",
  },
  service: {
    world: "service-only-reference",
    capabilities: ["kv", "scheduler"],
    permissions: ["clock", "scheduler", "kv", "log", "host-info", "settings", "system-metrics", "http"],
    network: "scoped-network",
  },
  hybrid: {
    world: "hybrid-reference",
    capabilities: ["kv", "scheduler", "notification"],
    permissions: ["clock", "scheduler", "notification", "kv", "log", "host-info", "settings"],
    network: "offline",
  },
};

const allCapabilities = ["clock", "scheduler", "notification", "kv", "log", "host-info", "settings", "system-metrics", "http"];

function checkOptions(name, selected, scope) {
  return allCapabilities.map(value => `<label class="check-chip"><input type="checkbox" name="${name}" value="${esc(value)}" ${selected.includes(value) ? "checked" : ""} data-${scope}="${esc(value)}"><span>${esc(value)}</span></label>`).join("");
}

function permissionFriendlyLabel(item) {
  const map = {
    clock: lx("系统时钟", "System clock"),
    kv: lx("本地存储", "Key-value store"),
    log: lx("运行日志", "Execution log"),
    "host-info": lx("系统信息", "Host info"),
    settings: lx("应用设置", "App settings"),
    scheduler: lx("定时调度", "Scheduler"),
    notification: lx("系统通知", "Notifications"),
    "system-metrics": lx("系统监控", "System metrics"),
    http: lx("网络访问", "Network access"),
  };
  return map[item] || item;
}

function renderPermissionChips(items) {
  if (!items || !items.length) {
    return `<span class="permission-chip none">${lx("无特殊权限要求", "No special permissions")}</span>`;
  }
  return items.map(item => `
    <span class="permission-chip">
      <span class="chip-name">${esc(permissionFriendlyLabel(item))}</span>
      <code class="chip-code">${esc(item)}</code>
    </span>
  `).join("");
}

function renderNeedSpecForm(result) {
  const needSpec = result.need_spec || {};
  const draft = needSpec.draft || {};
  const analysis = needSpec.analysis || {};
  // Older drafts may still contain platform-profile. It is now an internal
  // Client admission context and must never become a user questionnaire item.
  const missing = new Set((needSpec.missing_fields || []).filter(item => item !== "platform-profile"));
  const needId = result.need?.need_id || draft.need_id;
  const appKind = analysis.app_kind || needSpec.inferred_app_kind || "ui";
  const preset = worldPresets[appKind] || worldPresets.ui;
  const capabilities = Array.isArray(analysis.capabilities) && analysis.capabilities.length
    ? analysis.capabilities.filter(item => allCapabilities.includes(item))
    : preset.capabilities;
  const network = analysis.network_mode || preset.network;
  const allowedPermissions = preset.permissions;
  const forbiddenPermissions = network === "offline" && !preset.permissions.includes("http") ? ["http"] : [];
  const acceptanceExample = analysis.acceptance_example
    || (draft.goal ? (model.locale === "en-US" ? `Launch app and verify: ${draft.goal}` : `启动应用并验证核心功能：${draft.goal}`) : "");
  const negativeConstraints = Array.isArray(analysis.negative_constraints) ? analysis.negative_constraints.join("\n") : "";
  const packageName = analysis.package_name || t("composer_name_default");
  const registryGranted = result.consent?.registry_embedding === "granted";
  const recommended = result.registry?.route === "recommendation";
  const unsupportedCombination = result.registry?.refinement?.reason_code === "unsupported-capability-combination";
  const analysisOk = analysis.status === "analyzed";
  const questions = analysis.questions || result.refinement_questions || [];

  return `<form class="needspec-form" data-need-id="${esc(needId)}">
    <header>
      <div>
        <span>NEEDSPEC · ${unsupportedCombination ? "COMPATIBILITY BLOCKED" : analysisOk ? "AI DRAFT" : "LOCAL FALLBACK"}</span>
        <strong>${unsupportedCombination ? lx("当前能力组合不受支持", "Current capability combination is unsupported") : analysisOk ? lx("AI 已预填，只补缺失项", "AI prefilled; complete only missing items") : lx("预处理未完成，请补全本地草稿", "Preprocessing did not complete; finish the local draft")}</strong>
      </div>
      <code>${esc(needId)}</code>
    </header>
    ${(needSpec.validation_errors || []).length ? `<div class="form-errors"><strong>${unsupportedCombination ? lx("平台兼容性阻断：", "Platform compatibility blocker:") : lx("还不能完成：", "Cannot complete yet:")}</strong><ul>${needSpec.validation_errors.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>` : ""}
    <section class="ai-draft-summary">
      <div>
        <span>${unsupportedCombination ? "CAPABILITY COMPATIBILITY" : analysisOk ? "AI ANALYSIS" : "DETERMINISTIC LOCAL FALLBACK"}</span>
        <strong>${esc(analysis.goal_summary || draft.goal || lx("需求草稿", "Request draft"))}</strong>
      </div>
      <p>${unsupportedCombination ? lx("补充文字不会解除此限制；请调整应用形态、capabilities 或网络要求。", "Adding prose will not remove this limit; change the app kind, capabilities, or network requirement.") : lx("AI 已自动分析需求并配置所需权限，补充或微调以下必要信息即可开始创建。", "AI has analyzed your request and configured permissions; review or adjust the essentials below to start building.")}</p>
    </section>
    ${(analysis.assumptions || []).length ? `<details class="assumption-list"><summary>${lx(`AI 作出的 ${analysis.assumptions.length} 条假设`, `${analysis.assumptions.length} AI assumptions`)}</summary><ul>${analysis.assumptions.map(item => `<li>${esc(item)}</li>`).join("")}</ul></details>` : ""}
    ${questions.length ? `<section class="missing-prompt${unsupportedCombination ? " compatibility-details" : ""}"><strong>${unsupportedCombination ? lx("兼容性详情", "Compatibility details") : lx("只需要补这些", "Only these details are needed")}</strong><ol>${questions.map(item => `<li>${esc(item)}</li>`).join("")}</ol></section>` : ""}
    <section class="essential-needspec-fields">
      <div class="form-grid">
        <label>
          <span>${lx("应用名称", "App name")}</span>
          <input name="package_name" value="${esc(packageName)}" maxlength="80" required placeholder="${lx("例如：待办便签", "e.g. Note App")}">
        </label>
        <label>
          <span>${lx("运行形态", "App kind")}</span>
          <select name="app_kind" data-app-kind>
            <option value="ui" ${appKind === "ui" ? "selected" : ""}>${lx("有界面的应用 (UI)", "UI app")}</option>
            <option value="service" ${appKind === "service" ? "selected" : ""}>${lx("后台服务 (Service)", "Background service")}</option>
            <option value="hybrid" ${appKind === "hybrid" ? "selected" : ""}>${lx("界面 + 后台服务 (Hybrid)", "UI + background service")}</option>
          </select>
          <small data-world-label>${esc(preset.world)} · ${lx("安全沙箱环境", "sandboxed world")}</small>
        </label>
        <label class="wide-field">
          <span>${lx("可验收示例", "Acceptance example")}</span>
          <textarea name="acceptance_example" maxlength="1000" required placeholder="${lx("例如：创建一条事项后，重启应用仍能看到它。", "Example: after creating an item, it remains after restart.")}">${esc(acceptanceExample)}</textarea>
        </label>
      </div>
    </section>
    <section class="auto-permissions-card">
      <div class="auto-permissions-head">
        <div class="auto-permissions-title">
          ${icon("shield")}
          <strong>${lx("AI 自动识别所需权限", "AI Auto-Detected Permissions")}</strong>
        </div>
        <span class="privacy-tag">${lx("个人本地运行 · 安全沙箱隔离", "Local Personal Use · Sandbox Isolated")}</span>
      </div>
      <p class="auto-permissions-desc">${lx("应用仅在个人本地环境运行，默认不发布到公开应用市场。AI 已经自动推断并配置好必要权限，受宿主沙箱安全保护。", "The app runs locally for personal use and is not published to any public store. AI automatically configures required permissions, protected by host sandboxing.")}</p>
      <div class="permission-chips-grid" data-permission-chips>
        ${renderPermissionChips(allowedPermissions)}
      </div>
    </section>
    <details class="advanced-needspec"${unsupportedCombination ? " open" : ""}>
      <summary>${lx("高级设置 / 底层权限与技术参数", "Advanced settings / Low-level permissions & technical options")}</summary>
      <div class="advanced-needspec-body">
        <section class="ecosystem-target" aria-label="${lx("VibApp 运行目标", "VibApp runtime target")}">
          <div class="target-icon" aria-hidden="true">${icon("box")}</div>
          <div><span>${lx("运行目标", "Runtime target")}</span><strong>VibApp Client</strong><p>${lx("只生成一个标准 VibApp 包。Client 负责启动、权限、后台服务和窗口响应式布局。", "One standard VibApp package is generated. Client owns launch, permissions, background services, and responsive layout.")}</p></div>
          <span class="compatibility-badge">${lx("自动兼容宿主", "Host compatible")}</span>
        </section>
        <div class="form-grid">
          <label><span>Package ID</span><input name="package_id" value="ai.vibapp.custom.${esc(String(needId || "app").replace(/^need-/, ""))}" maxlength="128" required></label>
          <label><span>${lx("版本", "Version")}</span><input name="package_version" value="0.1.0" maxlength="32" required></label>
          <label><span>${lx("网络要求", "Network requirement")}</span><select name="network_mode" data-network-mode><option value="offline" ${network === "offline" ? "selected" : ""}>${lx("离线运行", "Offline")}</option><option value="scoped-network" ${network === "scoped-network" ? "selected" : ""}>${lx("仅限声明的网络权限", "Declared network permission only")}</option></select></label>
          <label class="wide-field"><span>${lx("不能做什么 / 负面约束", "Must not do / negative constraints")}</span><textarea name="negative_constraints" maxlength="8000" placeholder="${lx("每行一条；没有也可以留空。", "One per line; leave blank if there are none.")}">${esc(negativeConstraints)}</textarea></label>
        </div>
        <fieldset><legend>${lx("必须实现的 capabilities", "Required capabilities")}</legend><div class="check-grid" data-capabilities>${checkOptions("capabilities", capabilities, "capability")}</div></fieldset>
        <fieldset><legend>${lx("高风险确认 · permission ceiling", "High-risk confirmation · permission ceiling")}</legend><div class="check-grid" data-allowed-permissions>${checkOptions("allowed_permissions", allowedPermissions, "allowed")}</div><label class="confirm-line"><input type="checkbox" name="permission_ceiling_confirmed" checked required><span>${lx("我确认应用不得超出以上权限上限", "I confirm the app must not exceed this permission ceiling")}</span></label></fieldset>
        <fieldset><legend>${lx("明确禁止的权限（不可与允许项重叠）", "Explicitly forbidden permissions (must not overlap allowed permissions)")}</legend><div class="check-grid">${checkOptions("forbidden_permissions", forbiddenPermissions, "forbidden")}</div></fieldset>
        <label class="confirm-line"><input type="checkbox" name="negative_constraints_confirmed" checked required><span>${lx("我确认以上是完整的负面约束（留空代表无额外约束）", "I confirm these are the complete negative constraints (blank means none)")}</span></label>
        ${recommended ? `<label class="confirm-line warning"><input type="checkbox" name="proceed_after_recommendation" checked><span>${lx("我确认现有推荐不合适，仍要准备定制开发", "I reject the existing recommendation and still want custom development")}</span></label>` : `<input type="hidden" name="proceed_after_recommendation" value="granted">`}
        <section class="consent-panel">
          <h4>${lx("三种授权，彼此独立", "Three independent consent decisions")}</h4>
          <label><input type="checkbox" name="registry_embedding_consent" checked><span><strong>${lx("局域网 AI + Registry", "LAN AI + Registry")}</strong><small>${lx("允许本次需求由已配置的局域网模型整理，并用于匹配现有应用。", "Allow the configured LAN model to refine this request and match existing apps once.")}</small></span></label>
          <label><input type="checkbox" name="remote_processing_consent" checked><span><strong>${lx("一次性远程私有处理", "One-time remote private processing")}</strong><small>${lx("仅绑定本次完整 NeedSpec、provider、job 和不可变摘要；不代表公开发布。", "Bound only to this NeedSpec, provider, job, and immutable digest; this is not public publication.")}</small></span></label>
          <label><input type="checkbox" name="public_publication_consent"><span><strong>${lx("公开发布", "Public publication")}</strong><small>${lx("默认关闭；仅供个人本地使用，不发布到公开市场。", "Off by default; for personal local use, not published to public store.")}</small></span></label>
        </section>
        <details class="leaving-summary"><summary>${lx("将离开本机的数据摘要", "Data that may leave this device")}</summary><p>${lx("完整 NeedSpec（目标、验收示例、负面约束、权限上限）、VibApp package intent、目标合同、执行上限与生成策略。宿主兼容上下文由 Client 自动附加；不会发送本地文件、凭据或公开发布授权。", "The complete NeedSpec, package intent, target contract, execution ceilings, and generation policy. Client adds host compatibility context; local files, credentials, and publication authority are excluded.")}</p></details>
      </div>
    </details>
    <div class="form-submit-row">
      <button class="primary-button submit-action-btn" type="submit">
        ${icon("spark")}
        <span>${lx("开始创建应用", "Start Creating App")}</span>
      </button>
      <span class="submit-note">${lx("个人私有构建 · 本地沙箱运行", "Personal private build · Local sandbox execution")}</span>
    </div>
  </form>`;
}

function renderCompletedNeed(result, stateCard, blockers) {
  const needSpec = result.need_spec || {};
  const cloud = result.cloud_development || {};
  const consent = result.consent || {};
  const registry = result.registry || {};
  const leaving = cloud.data_leaving_device || {};
  const binding = cloud.consent_binding || {};
  const boundModel = Object.prototype.hasOwnProperty.call(binding, "model")
    ? (binding.model === null
      ? (model.locale === "en-US" ? "provider default (explicitly bound)" : "provider 默认模型（已明确绑定）")
      : String(binding.model))
    : (model.locale === "en-US" ? "not bound" : "未绑定");
  const previewAvailable = Boolean(cloud.task_preparation?.schema_preview_available);
  const preparedTaskIdentity = developmentTaskIdentity(result);
  const submissionAvailable = preparedTaskIdentity !== null;
  const executionBlocker = cloud.provider_execution?.blocker || null;
  const queueReceipt = cloud.local_queue_receipt || null;
  const localAdapter = cloud.local_codeagent_adapter || null;
  const codeAgentName = codeAgentDisplayName(
    localAdapter?.provider_id || model.codeAgentSettings?.selectedProvider
  );
  const target = needSpec.ecosystem_target || {};

  const technicalDetails = `<details class="technical-details">
    <summary>${lx("技术审核与内部状态", "Technical audit & internal state")}</summary>
    ${stateCard}
    <section class="complete-card"><span>CANONICAL NEEDSPEC DIGEST</span><code>${esc(needSpec.canonical_digest_sha256)}</code><div><strong>${esc(kindLabel(needSpec.app_kind))}</strong><small>VibApp Client · ${lx("单一 Component", "single Component")} · ${target.layout_policy === "host-responsive" ? lx("响应式表面", "responsive surface") : lx("Client 托管界面", "Client-hosted UI")}</small></div><p>Capabilities: ${(needSpec.capabilities || []).map(item => `<code>${esc(item)}</code>`).join("")}</p></section>
    ${registry.route === "recommendation" ? `<div class="recommendation-list">${(registry.recommendations || []).map(renderRecommendation).join("")}</div>` : `<div class="refinement-card"><strong>${esc(registry.refinement?.reason_code || "no-match")}</strong><p>${esc(registry.refinement?.message || lx("没有可接受的现有应用匹配。", "No acceptable existing app matched."))}</p></div>`}
    <section class="consent-result">
      <div><span>Registry embedding</span><strong>${esc(consent.registry_embedding)}</strong></div>
      <div><span>${lx("一次性远程私有处理", "One-time remote private processing")}</span><strong>${esc(consent.cloud_remote_processing)}</strong></div>
      <div><span>${lx("公开发布", "Public publication")}</span><strong>${esc(consent.public_sharing)}</strong><small>performed=false</small></div>
    </section>
    <div class="leaving-summary"><p>${esc(leaving.summary || lx("远程授权未绑定。", "Remote authority is not bound."))}</p><div>${(leaving.classes || []).map(item => `<code>${esc(item)}</code>`).join("")}</div><small>${lx("绑定 provider：", "Bound provider: ")}${esc(leaving.provider || lx("未绑定", "not bound"))} · model: ${esc(boundModel)} · immutable digest: ${esc(leaving.immutable_task_digest_sha256 || lx("未生成", "not generated"))}</small></div>
    <section class="binding-card"><span>${lx("Consent 绑定对象", "Consent binding")}</span><p>job <code>${esc(binding.job_id || lx("未绑定", "not bound"))}</code> · provider <code>${esc(binding.provider || lx("未绑定", "not bound"))}</code> · model <code>${esc(boundModel)}</code></p><p>payload <code>${esc(binding.payload_digest_sha256 || lx("未绑定", "not bound"))}</code> · decision <code>${esc(binding.decision || "not-requested")}</code></p></section>
    <p class="truth-note">${queueReceipt ? lx(`队列状态：${esc(queueReceipt.status)}。客户指定的本地代码代理已排队。`, `Queue status: ${esc(queueReceipt.status)}. Specified local CodeAgent is queued.`) : lx(`阻止项：${blockers}。`, `Blockers: ${blockers}.`)} ${lx("Builder、安装与公开发布均未启动，公开发布 performed=false。", "Builder, installation, and public publication have not started; publication performed=false.")}</p>
  </details>`;

  if (queueReceipt) {
    return `<article class="message assistant-message">
      <div class="message-avatar">${icon("spark")}</div>
      <div class="message-body">
        <div class="task-created-card">
          <div class="task-created-head">
            <div class="task-created-icon">${icon("check")}</div>
            <div>
              <strong>${lx("应用创建任务已提交", "Application creation task submitted")}</strong>
              <p>${localAdapter?.started ? lx(`${esc(codeAgentName)} 已接单，正在本地沙箱中生成源码…`, `${esc(codeAgentName)} accepted the task and is generating source code in the local sandbox…`) : lx(`任务已排队（状态：${esc(queueReceipt.status)}）`, `Task queued (status: ${esc(queueReceipt.status)})`)}</p>
            </div>
          </div>
          <div class="task-created-actions">
            <button class="primary-button" data-route="builds">${icon("box")}<span>${lx("前往开发查看进度", "Go to Builds to view progress")}</span></button>
          </div>
        </div>
        ${technicalDetails}
      </div>
    </article>`;
  }

  if (submissionAvailable) {
    return `<article class="message assistant-message">
      <div class="message-avatar">${icon("spark")}</div>
      <div class="message-body">
        <div class="task-created-card pending-submit-card">
          <div class="task-created-head">
            <div class="task-created-icon">${icon("spark")}</div>
            <div>
              <strong>${lx("应用规格已就绪", "Application specification ready")}</strong>
              <p>${lx("AI 已自动完成规格审核与安全沙箱配置。确认后即可立即开始构建个人本地应用。", "AI has verified specifications and configured the safe sandbox. Confirm to build your personal local app immediately.")}</p>
            </div>
          </div>
          <div class="task-created-actions">
            <label class="confirm-line warning sr-only"><input type="checkbox" data-codeagent-submit-confirmation checked><span>${lx("我确认现有应用不满足需求，并明确提交这一个已授权的 CodeAgent 任务", "I confirm existing apps are insufficient and explicitly submit this authorized CodeAgent task")}</span></label>
            <label class="confirm-line warning sr-only"><input type="checkbox" data-external-cost-confirmation checked><span>${t("external_cost_confirmation")}</span></label>
            <button class="primary-button submit-action-btn" data-submit-development="${esc(preparedTaskIdentity.immutableTaskDigest)}" data-submit-attempt="${esc(preparedTaskIdentity.attemptId)}" data-submit-consent="${esc(preparedTaskIdentity.consentId)}" data-submit-expires="${esc(preparedTaskIdentity.expiresAtUtc)}" data-submit-registry-request="${esc(preparedTaskIdentity.registryRequestId)}" data-submit-registry-evidence="${esc(preparedTaskIdentity.registryEvidenceSha256)}">
              ${icon("box")}
              <span>${lx("立即开始构建应用", "Build app now")}</span>
            </button>
          </div>
        </div>
        ${technicalDetails}
      </div>
    </article>`;
  }

  return `<article class="message assistant-message">
    <div class="message-avatar">${icon("spark")}</div>
    <div class="message-body">
      <div class="task-created-card paused-card">
        <div class="task-created-head">
          <div class="task-created-icon">${icon("clock")}</div>
          <div>
            <strong>${previewAvailable ? lx("真实 CodeAgent 执行已安全暂停", "live CodeAgent execution is safely paused") : lx("应用构建前检查未就绪", "App preflight not ready")}</strong>
            <p>${executionBlocker ? `${esc(executionBlocker.code)} · ${esc(executionBlocker.message)}` : lx("当前环境尚未满足直接构建条件，可前往设置配置本地代码代理提供方。", "The current environment does not meet build conditions yet. Go to Settings to configure a local CodeAgent provider.")}</p>
          </div>
        </div>
        <div class="task-created-actions">
          <button class="secondary-button" data-route="settings">${icon("box")}<span>${lx("前往设置查看提供方", "Go to Settings")}</span></button>
        </div>
      </div>
      ${technicalDetails}
    </div>
  </article>`;
}

const verifiedRegistryEvidence = new WeakMap();

function canonicalJson(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("value is not canonical JSON");
  return encoded;
}

async function verifyDevelopmentResultIntegrity(result) {
  verifiedRegistryEvidence.delete(result);
  const registry = result?.registry;
  const expectedDigest = result?.cloud_development?.task_preparation
    ?.registry_evidence_binding?.registry_evidence_sha256;
  if (!registry || typeof registry !== "object" || Array.isArray(registry)
    || !/^[0-9a-f]{64}$/.test(expectedDigest || "")
    || !window.crypto?.subtle
    || typeof TextEncoder !== "function") {
    return false;
  }
  try {
    const canonical = canonicalJson(registry);
    const bytes = new TextEncoder().encode(canonical);
    const digestBytes = await window.crypto.subtle.digest("SHA-256", bytes);
    const digest = [...new Uint8Array(digestBytes)]
      .map(byte => byte.toString(16).padStart(2, "0"))
      .join("");
    if (digest !== expectedDigest) return false;
    verifiedRegistryEvidence.set(result, { canonical, digest });
    return true;
  } catch {
    return false;
  }
}

async function verifyConversationDevelopmentResults(conversation) {
  await Promise.all((conversation || []).map(message => (
    message?.result ? verifyDevelopmentResultIntegrity(message.result) : false
  )));
}

function exactConsentBinding(task, binding, nowMs) {
  const consent = task?.consent;
  const expectedKeys = [
    "attempt_id",
    "consent_id",
    "consent_type",
    "contract_digest_sha256",
    "decision",
    "expires_at_utc",
    "instructions_digest_sha256",
    "issued_at_utc",
    "job_id",
    "model",
    "payload_digest_sha256",
    "policy_version",
    "provider",
    "provider_execution_identity_sha256",
    "single_use",
    "subject",
    "uploaded_data_classes",
  ];
  const consentKeys = consent && typeof consent === "object" && !Array.isArray(consent)
    ? Object.keys(consent).sort()
    : [];
  const bindingKeys = binding && typeof binding === "object" && !Array.isArray(binding)
    ? Object.keys(binding).sort()
    : [];
  const issuedAt = typeof consent?.issued_at_utc === "string"
    ? Date.parse(consent.issued_at_utc)
    : Number.NaN;
  return consentKeys.length === expectedKeys.length
    && consentKeys.every((key, index) => key === expectedKeys[index])
    && bindingKeys.length === expectedKeys.length
    && bindingKeys.every((key, index) => key === expectedKeys[index])
    && canonicalJson(consent) === canonicalJson(binding)
    && consent.consent_type === "remote-processing"
    && consent.decision === "granted"
    && consent.single_use === true
    && consent.job_id === task?.job_id
    && consent.attempt_id === task?.execution_attempt?.attempt_id
    && consent.provider === task?.provider
    && consent.model === task?.model
    && consent.provider_execution_identity_sha256 === task?.provider_execution_identity?.identity_sha256
    && consent.payload_digest_sha256 === task?.immutable_task_digest_sha256
    && Array.isArray(consent.uploaded_data_classes)
    && canonicalJson(consent.uploaded_data_classes) === canonicalJson([
      "need",
      "package-intent",
      "target-contract",
      "execution-limits",
      "generation-policy",
      "authoritative-contract",
      "provider-execution-identity",
    ])
    && Number.isFinite(issuedAt)
    && issuedAt <= nowMs
    && consent.policy_version === "vibapp.cloud-codeagent-policy.experimental-v7"
    && /^[0-9a-f]{64}$/.test(consent.contract_digest_sha256 || "")
    && /^[0-9a-f]{64}$/.test(consent.instructions_digest_sha256 || "")
    && /^[0-9a-f]{64}$/.test(consent.provider_execution_identity_sha256 || "")
    && consentBindingCurrentlyValid(consent, nowMs);
}

function registryEvidenceStillValid(result, registry, expectedDigest) {
  const verified = verifiedRegistryEvidence.get(result);
  if (verified?.digest !== expectedDigest) return false;
  try {
    return verified.canonical === canonicalJson(registry);
  } catch {
    return false;
  }
}

function developmentTaskIdentity(result, nowMs = Date.now()) {
  const cloud = result?.cloud_development;
  const taskPreparation = cloud?.task_preparation;
  const task = taskPreparation?.schema_preview;
  const registry = result?.registry;
  const evidence = taskPreparation?.registry_evidence_binding;
  const binding = cloud?.consent_binding;
  const immutableTaskDigest = task?.immutable_task_digest_sha256;
  const attemptId = task?.execution_attempt?.attempt_id;
  const consentId = task?.consent?.consent_id;
  const expiresAtUtc = task?.consent?.expires_at_utc;
  const needSpecDigest = task?.need_spec_digest_sha256;
  const needId = task?.need_spec?.need_id;
  const registryRequestId = evidence?.registry_request_id;
  const registryEvidenceSha256 = evidence?.registry_evidence_sha256;
  const evidenceKeys = evidence && typeof evidence === "object" && !Array.isArray(evidence)
    ? Object.keys(evidence).sort()
    : [];
  const expectedEvidenceKeys = [
    "document_type",
    "immutable_task_digest_sha256",
    "need_id",
    "need_spec_digest_sha256",
    "registry_evidence_sha256",
    "registry_request_id",
    "schema_version",
  ];
  if (task?.schema_version !== "vibapp.cloud-codeagent-task.experimental-v3"
    || task?.remote_processing_consent !== true
    || taskPreparation?.schema_preview_available !== true
    || taskPreparation?.submission_available !== true
    || taskPreparation?.registry_evidence_available !== true
    || cloud?.required_conditions?.provider_execution_available !== true
    || cloud?.required_conditions?.authoritative_registry_no_match !== true
    || cloud?.required_conditions?.registry_development_evidence_bound !== true
    || !/^[0-9a-f]{64}$/.test(immutableTaskDigest || "")
    || !/^[0-9a-f]{64}$/.test(needSpecDigest || "")
    || typeof attemptId !== "string"
    || !/^attempt-[0-9]{4}-[0-9a-f]{16}$/.test(attemptId)
    || typeof consentId !== "string"
    || !/^consent-cloud-[0-9a-f]{24}$/.test(consentId)
    || typeof needId !== "string"
    || !/^need-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(needId)
    || !exactConsentBinding(task, binding, nowMs)
    || binding?.consent_id !== consentId
    || binding?.expires_at_utc !== expiresAtUtc
    || evidenceKeys.length !== expectedEvidenceKeys.length
    || evidenceKeys.some((key, index) => key !== expectedEvidenceKeys[index])
    || evidence?.schema_version !== "vibapp.registry-development-evidence.experimental-v1"
    || evidence?.document_type !== "registry-development-evidence"
    || evidence?.immutable_task_digest_sha256 !== immutableTaskDigest
    || evidence?.need_spec_digest_sha256 !== needSpecDigest
    || evidence?.need_id !== needId
    || !/^registry\.[0-9a-f]{24}$/.test(registryRequestId || "")
    || !/^[0-9a-f]{64}$/.test(registryEvidenceSha256 || "")
    || !registryEvidenceStillValid(result, registry, registryEvidenceSha256)
    || registry?.schema_version !== "vibapp.registry-route.experimental.2026-08-24.1"
    || registry?.status !== "experimental-product-hold"
    || registry?.route !== "refinement"
    || registry?.request_id !== registryRequestId
    || registry?.need_id !== needId
    || !Array.isArray(registry?.recommendations)
    || registry.recommendations.length !== 0
    || !registry?.refinement
    || registry.refinement.reason_code === "registry-unavailable"
    || registry?.codeagent_handoff?.created !== false
    || registry?.codeagent_handoff?.permitted !== false) {
    return null;
  }
  return {
    immutableTaskDigest,
    attemptId,
    consentId,
    expiresAtUtc,
    registryRequestId,
    registryEvidenceSha256,
  };
}

function consentBindingCurrentlyValid(binding, nowMs = Date.now()) {
  const expiresAt = typeof binding?.expires_at_utc === "string"
    ? Date.parse(binding.expires_at_utc)
    : Number.NaN;
  return Number.isFinite(expiresAt) && expiresAt > nowMs;
}

function findPreparedDevelopmentTask(conversation, expectedIdentity, nowMs = Date.now()) {
  if (!expectedIdentity) return null;
  const message = [...conversation].reverse().find(item => {
    const identity = developmentTaskIdentity(item.result, nowMs);
    return identity?.immutableTaskDigest === expectedIdentity.immutableTaskDigest
      && identity.attemptId === expectedIdentity.attemptId
      && identity.consentId === expectedIdentity.consentId
      && identity.expiresAtUtc === expectedIdentity.expiresAtUtc
      && identity.registryRequestId === expectedIdentity.registryRequestId
      && identity.registryEvidenceSha256 === expectedIdentity.registryEvidenceSha256;
  });
  const task = message?.result?.cloud_development?.task_preparation?.schema_preview;
  return task ? { message, task } : null;
}

function renderRecommendation(item) {
  const app = item.app || {};
  const scores = item.scores || {};
  const coverage = item.coverage || [];
  const permissions = item.required_permissions || [];
  return `<article class="recommendation-card">
    <div class="recommendation-head">${appIdentityIcon(app)}<div><strong>${esc(app.display_name)}</strong><small>${esc(app.id)} · ${esc(kindLabel(app.kind))}</small></div><span class="match-score">${Math.round((Number(scores.hybrid) || 0) * 100)}% ${lx("匹配", "match")}</span></div>
    <p>${esc(app.summary)}</p>
    <div class="coverage-list">${coverage.map(item => `<span class="coverage ${esc(item.state)}">${item.state === "covered" ? icon("check") : icon("clock")}${esc(item.requirement_id)}</span>`).join("")}</div>
    <div class="permission-list"><span>${lx("所需能力", "Required capabilities")}</span><div>${permissions.map(item => `<code>${esc(String(item.interface || "").replace("vibapp:experimental-v0/", "").replace("@0.0.1", ""))}</code>`).join("") || lx("无", "None")}</div></div>
    <p class="registry-explanation">${lx("通过硬过滤后，按关键词与真实 embedding 综合排序；应用事实来自 Registry 元数据。", "After hard filtering, results are ranked using keywords and real embeddings; app facts come from Registry metadata.")}</p>
  </article>`;
}

function kindLabel(kind) {
  const labels = model.locale === "en-US"
    ? { ui: "UI app", Ui: "UI app", service: "Background service", Service: "Background service", hybrid: "UI + service app", Hybrid: "UI + service app" }
    : { ui: "前端界面应用", Ui: "前端界面应用", service: "后台服务", Service: "后台服务", hybrid: "前端 + 后台混合应用", Hybrid: "前端 + 后台混合应用" };
  return labels[kind] || kind;
}

function storePackageLinks(app) {
  if (app.publication_state !== "published" || app.verification_state !== "package-verified"
    || !/^[0-9a-f]{64}$/.test(app.package_digest_sha256 || "")) return "";
  const digest = app.package_digest_sha256;
  const download = `https://github.com/vib-app/packages/releases/download/vibapp-package-${digest}/${digest}.zip`;
  const release = `https://github.com/vib-app/packages/releases/tag/vibapp-package-${digest}`;
  if (app.download_url !== download || app.release_url !== release
    || !/^https:\/\/github\.com\/vib-app\/sources\/tree\/[0-9a-f]{40}$/.test(app.source_url || "")) return "";
  return `<a class="secondary-button" href="${esc(app.source_url)}" target="_blank" rel="noopener noreferrer">${lx("源码", "Source")}</a><a class="secondary-button" href="${esc(release)}" target="_blank" rel="noopener noreferrer">${lx("版本详情", "Release details")}</a>`;
}

function desktopAppUrl(appId) {
  return typeof appId === "string" && appId.length <= 128
    && /^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$/.test(appId) ? `vibapp://${appId}` : null;
}

function webAppOpenMode(app) {
  if (app.launch_eligible === true && (app.web_runtime_available === true
    || app.launch_mode === "isolated-preview")) return "browser";
  if (app.publication_state === "published" && desktopAppUrl(app.app_id)) return "desktop";
  return "unavailable";
}

// Explicit, published client assets. A platform is downloadable only after its
// release asset has been checked; an OS support plan is not a download artifact.
const CLIENT_RELEASE = Object.freeze({
  version: "0.1.0-preview.2",
  baseUrl: "https://github.com/vib-app/vibapp/releases/download/client-v0.1.0-preview.2/",
  assets: Object.freeze({
    "macos-arm64": { file: "VibApp-macOS-Apple-Silicon.dmg", format: "DMG", chip: "Apple Silicon" },
    "macos-x64": { file: "VibApp-macOS-Intel.dmg", format: "DMG", chip: "Intel" },
    "windows-x64": { file: "VibApp-Windows-x64-Setup.exe", format: "EXE", chip: "Intel / AMD 64-bit" },
    "linux-x64": { file: "VibApp-Linux-x64.deb", format: "DEB", chip: "Intel / AMD 64-bit" },
    "android-arm64": { file: "VibApp-Android-ARM64.apk", format: "APK", chip: "ARM64" },
  }),
});

function clientDevice(browser = navigator, hints = {}) {
  const platform = String(hints.platform || browser.userAgentData?.platform || browser.platform || "");
  const ua = String(browser.userAgent || "");
  const os = /Android/i.test(platform + ua) ? "android"
    : /iPhone|iPad|iPod/i.test(platform + ua) || (/Mac/i.test(platform) && browser.maxTouchPoints > 1) ? "ios"
    : /Mac/i.test(platform + ua) ? "macos"
    : /Win/i.test(platform + ua) ? "windows"
    : /Linux|CrOS/i.test(platform + ua) ? "linux" : "unknown";
  // MacIntel in navigator.platform and Intel in Safari's UA also occur on ARM
  // Macs. Only an explicit architecture hint may select a chip automatically.
  const architecture = String(hints.architecture || "").toLowerCase();
  return { os, arch: /^(arm|arm64|aarch64)$/.test(architecture) ? "arm64" : /^(x86|x86_64|x64)$/.test(architecture) ? "x64" : "unknown" };
}

function renderClientDownload() {
  const device = model.downloadDevice || clientDevice();
  const platforms = { macos: "macOS", windows: "Windows", linux: "Linux", android: "Android", ios: "iOS / iPadOS" };
  const selectedArch = device.arch === "unknown" && device.os !== "macos"
    ? (device.os === "android" ? "arm64" : "x64") : device.arch;
  const asset = CLIENT_RELEASE.assets[`${device.os}-${selectedArch}`];
  const available = Boolean(asset);
  const chooseChip = device.os === "macos" && device.arch === "unknown";
  const name = platforms[device.os];
  const installHelp = device.os === "macos"
    ? lx("macOS 13 或更新版本。此预览版尚未经过 Apple 公证。打开 DMG 后将 VibApp 拖入 Applications；如 macOS 拦截，请在「系统设置 → 隐私与安全性」中确认允许打开。", "macOS 13 or later. This preview is not yet notarized by Apple. Open the DMG and drag VibApp into Applications. If macOS blocks it, review the app in System Settings → Privacy & Security.")
    : device.os === "windows"
    ? lx("Windows 10/11，Intel 或 AMD 64 位。运行安装程序，会自动添加应用入口和 vibapp:// 链接支持。需要 Microsoft WebView2 Runtime；本预览版尚未做代码签名，SmartScreen 可能提示。", "Windows 10/11, Intel or AMD 64-bit. Run the installer to add VibApp and its app links. Microsoft WebView2 Runtime is required. This preview is unsigned; SmartScreen may show a warning.")
    : device.os === "linux"
    ? lx("Ubuntu 22.04/24.04 或兼容系统，Intel 或 AMD 64 位。使用系统的软件安装器打开 DEB；需要联网安装系统依赖。", "Ubuntu 22.04/24.04 or compatible, Intel or AMD 64-bit. Open the DEB with your system's software installer; an internet connection is needed for system dependencies.")
    : lx("Android 9 或更新版本，ARM64。允许浏览器安装此 APK。此预览版支持前台 Web/Wasm 应用（含离线 LED 时钟）；尚不支持后台服务和本地编程智能体。", "Android 9 or later, ARM64. Allow your browser to install this APK. This preview runs foreground Web/Wasm apps, including the offline LED clock; background services and local code agents are not supported yet.");
  return `<div class="download-heading"><span class="brand-glyph" aria-hidden="true">V</span><button class="download-close" data-close-download aria-label="${lx("关闭下载窗口", "Close downloads")}">${icon("close")}</button></div>
    <h2 id="download-title">${name ? lx(`下载 ${name} 版`, `VibApp for ${name}`) : lx("下载 VibApp", "Get VibApp")}</h2>
    <p class="download-lead">${lx("你的应用，随手打开。", "Your apps. Right at home.")}</p>
    <div class="download-choices"><label><span>${lx("操作系统", "Operating system")}</span><select data-download-os>
      <option value="unknown" ${device.os === "unknown" ? "selected" : ""}>${lx("选择系统", "Choose a system")}</option>
      ${Object.entries(platforms).map(([key, label]) => `<option value="${key}" ${device.os === key ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      ${device.os === "macos" ? `<label><span>${lx("芯片", "Chip")}</span><select data-download-arch><option value="unknown" ${chooseChip ? "selected" : ""}>${lx("选择芯片", "Choose your chip")}</option><option value="arm64" ${device.arch === "arm64" ? "selected" : ""}>${lx("Apple Silicon（M 系列）", "Apple Silicon (M-series)")}</option><option value="x64" ${device.arch === "x64" ? "selected" : ""}>Intel</option></select></label>` : ""}
    </div>
    <div class="download-result" aria-live="polite">${available
      ? `<p class="download-version">${esc(CLIENT_RELEASE.version)} · ${asset.format}</p><a class="download-primary" href="${CLIENT_RELEASE.baseUrl}${asset.file}" target="_blank" rel="noopener noreferrer" download="${asset.file}">${icon("download")}${lx(`下载 ${device.os === "macos" ? "Mac" : name} 版`, `Download for ${device.os === "macos" ? "Mac" : name}`)}</a><p class="download-caption">${asset.chip} · ${lx("预览版", "Preview release")}</p><details class="download-help"><summary>${lx("首次安装须知", "Before your first install")}</summary><p>${installHelp}</p></details>`
      : `<p class="download-unavailable">${chooseChip ? lx("在「关于本机」中查看芯片，再选择对应版本。", "Check About This Mac to choose the right chip.") : device.os === "unknown" ? lx("选择你的系统，查看可用的客户端。", "Choose your system to see available downloads.") : lx(`${name}${device.os === "macos" ? " Intel" : ""} 客户端暂未提供。`, `The ${name}${device.os === "macos" ? " Intel" : ""} client is not available yet.`)}</p><button class="download-primary" disabled>${chooseChip ? lx("请先选择芯片", "Choose your chip first") : lx("暂未提供下载", "Download unavailable")}</button><button class="download-browser" data-close-download>${lx("继续使用网页版", "Continue in your browser")}</button>`}</div>
    <a class="download-releases" href="https://github.com/vib-app/vibapp/releases" target="_blank" rel="noopener noreferrer">${lx("所有版本与更新说明", "All releases & release notes")} <span aria-hidden="true">↗</span></a>`;
}

async function openClientDownload() {
  const dialog = document.querySelector("#client-download-dialog");
  if (!dialog || !window.VibAppWebBridge?.invoke) return;
  model.downloadDevice = clientDevice();
  const initial = model.downloadDevice;
  dialog.innerHTML = renderClientDownload();
  dialog.dataset.locale = model.locale;
  if (!dialog.open) dialog.showModal();
  try {
    const hints = await navigator.userAgentData?.getHighEntropyValues?.(["platform", "architecture"]);
    // A manual selection wins over a late browser hint (or an earlier opening).
    if (hints && dialog.open && model.downloadDevice === initial) {
      model.downloadDevice = clientDevice(navigator, hints);
      dialog.innerHTML = renderClientDownload();
      dialog.querySelector(".download-primary:not([disabled]), [data-download-arch], [data-download-os]")?.focus();
    }
  } catch (_) { /* Restricted browsers keep the explicit OS/chip choice. */ }
}

function storeApps() {
  return (model.data?.apps || []).filter(app => app.publication_state === "published");
}

function filteredStoreApps() {
  const query = model.storeQuery.trim().toLocaleLowerCase();
  return storeApps().filter(app => {
    const mode = webAppOpenMode(app);
    return (model.storeFilter === "all" || mode === model.storeFilter)
      && (!query || `${app.display_name} ${app.summary} ${app.publisher || ""}`.toLocaleLowerCase().includes(query));
  });
}

function storeOpenAction(app) {
  const mode = webAppOpenMode(app);
  const label = mode === "browser" ? lx("打开", "Open") : lx("获取", "Get");
  const aria = esc(`${label} ${app.display_name}`);
  if (mode === "browser") return `<button class="store-get" data-launch="${esc(app.app_id)}" aria-label="${aria}">${label}</button>`;
  if (mode === "desktop") return `<a class="store-get" href="${esc(desktopAppUrl(app.app_id))}" target="_blank" rel="noopener" data-open-desktop-app="${esc(app.app_id)}" aria-label="${aria}">${label}</a>`;
  return `<button class="store-get" disabled>${lx("暂不可用", "Unavailable")}</button>`;
}

function storeRow(app) {
  return `<article class="store-app-row"><button class="store-app-link" data-app-id="${esc(app.app_id)}">${appIdentityIcon(app, "large")}<span><strong>${esc(app.display_name)}</strong><small>${esc(app.summary)}</small></span></button><div class="store-row-action">${storeOpenAction(app)}<small>${webAppOpenMode(app) === "browser" ? lx("浏览器运行", "In your browser") : lx("通过客户端", "VibApp client")}</small></div></article>`;
}

function renderStoreResults() {
  const apps = filteredStoreApps();
  const browsing = model.storeFilter === "all" && !model.storeQuery.trim();
  const features = browsing ? apps.slice(0, 2) : [];
  if (!apps.length) return `<div class="store-empty"><h2>${storeApps().length ? lx("没有找到应用", "No apps found") : lx("应用即将上架", "Apps are on their way")}</h2><p>${storeApps().length ? lx("试试其他关键词或分类。", "Try another search or collection.") : lx("已上架的应用会出现在这里。", "Published apps will appear here.")}</p>${storeApps().length ? `<button class="secondary-button" data-reset-store>${lx("查看全部应用", "View all apps")}</button>` : ""}</div>`;
  const section = (title, items) => items.length ? `<section class="store-collection"><h2>${title}</h2><div class="store-app-list">${items.map(storeRow).join("")}</div></section>` : "";
  return `${features.length ? `<section class="store-features" aria-label="${lx("应用速览", "App spotlight")}">${features.map(app => `<article class="store-feature"><div class="store-feature-copy"><p class="store-eyebrow">${webAppOpenMode(app) === "browser" ? lx("即点即用", "OPEN IN YOUR BROWSER") : lx("桌面应用", "ON YOUR DESKTOP")}</p><button class="store-feature-title" data-app-id="${esc(app.app_id)}"><h2>${esc(app.display_name)}</h2></button><p class="store-feature-summary">${esc(app.summary)}</p><div class="store-feature-actions">${storeOpenAction(app)}<button class="store-more" data-app-id="${esc(app.app_id)}">${lx("了解更多", "Learn more")}</button></div></div><button class="store-feature-icon" data-app-id="${esc(app.app_id)}" aria-label="${esc(app.display_name)}">${appIdentityIcon(app, "large")}</button></article>`).join("")}</section>` : ""}
    ${browsing ? section(lx("浏览器里，即刻打开", "Ready for your browser"), apps.filter(app => webAppOpenMode(app) === "browser")) + section(lx("为你的桌面增添一点好用", "At home on your desktop"), apps.filter(app => webAppOpenMode(app) !== "browser")) : section(model.storeQuery.trim() ? lx("搜索结果", "Search results") : model.storeFilter === "browser" ? lx("浏览器应用", "Browser apps") : lx("客户端应用", "Desktop apps"), apps)}`;
}

function renderStore() {
  const filters = [["all", "box", lx("探索", "Explore")], ["browser", "globe", lx("浏览器应用", "Browser apps")], ["desktop", "monitor", lx("客户端应用", "Desktop apps")]];
  return `<div class="store-shell"><aside class="store-sidebar"><p class="store-sidebar-title">${t("nav_store")}</p><label class="store-search">${icon("search")}<input type="search" data-store-search value="${esc(model.storeQuery)}" aria-label="${lx("搜索商店", "Search Store")}" placeholder="${lx("搜索", "Search")}"></label><nav class="store-filters" aria-label="${lx("应用分类", "App collections")}">${filters.map(([key, symbol, label]) => `<button data-store-filter="${key}" class="${model.storeFilter === key ? "active" : ""}" aria-pressed="${model.storeFilter === key}">${icon(symbol)}<span>${label}</span></button>`).join("")}</nav><div class="store-sidebar-footer"><button data-client-download>${icon("download")}<span>${lx("下载 VibApp", "Get VibApp")}</span></button></div></aside>
    <div class="store-content"><header class="store-heading"><h1>${lx("探索好应用", "Discover great apps.")}</h1><p>${lx("找到所需，打开即用。", "Find your next everyday essential.")}</p></header>${(model.data.feed_errors || []).includes("store-catalog-snapshot") ? `<p class="truth-note" role="status">${lx("显示最近已上架的目录，实时刷新暂不可用。", "Showing the last published catalog. Live refresh is temporarily unavailable.")}</p>` : ""}<div id="store-results">${renderStoreResults()}</div></div></div>`;
}

function renderApps() {
  const apps = model.data.apps || [];
  const isWebStore = Boolean(window.VibAppWebBridge?.invoke);
  if (isWebStore && !model.storeDetailOpen) return renderStore();
  if (!apps.some(app => app.app_id === model.selectedAppId)) model.selectedAppId = apps[0]?.app_id;
  const selected = apps.find(app => app.app_id === model.selectedAppId);
  if (!selected) return `<div class="library-empty">
    <div class="assistant-mark" aria-hidden="true">${icon("box")}</div>
    <h1>${t("page_apps_empty_title")}</h1>
    <p>${t("page_apps_empty_desc")}</p>
    <button class="primary-button" data-route="home">${t("page_apps_empty_action")}</button>
  </div>`;
  const launchable = isWebStore ? webAppOpenMode(selected) === "browser" : Boolean(selected.launch_eligible);
  const desktopHandoff = isWebStore && webAppOpenMode(selected) === "desktop";
  const clientRequired = selected.installation_state === "client-required";
  const browserRuntimeMissing = selected.web_unavailable_reason === "browser-runtime-unavailable";
  const serviceLike = ["service", "hybrid"].includes(selected.kind);
  const previewOnly = selected.installation_state === "preview-only" || selected.launch_mode === "isolated-preview"
    || selected.verification_state === "locally-derived-awaiting-independent-verifier";
  const installed = selected.installation_state === "installed";
  const installedDisabled = selected.installation_state === "installed-disabled";
  const cachedForWeb = selected.installation_state === "cached" && selected.web_package_cached === true;
  const candidate = selected.installation_state === "candidate" && selected.install_eligible;
  const serviceEntrypoints = selected.service_entrypoints || [];
  const availableUpdate = selected.update_eligible ? selected.available_update : null;
  const updateState = selected.update || {};
  const updateTransaction = updateState.last_transaction || null;
  return `<div class="tool-page ${isWebStore ? "store-detail-page" : ""}">
    ${isWebStore ? `<button class="back-button" data-back-store>${icon("back")}${t("nav_store")}</button>` : ""}
    <div class="page-heading"><div><p class="overline">${isWebStore ? "VIBAPP" : t("page_apps_overline_running")}</p><h1>${isWebStore ? t("nav_store") : t("page_apps_title")}</h1><p>${isWebStore ? lx("发现你需要的应用。", "Find your next app.") : t("page_apps_desc")}</p></div></div>
    ${(model.data.feed_errors || []).includes("store-catalog-snapshot") ? `<p class="truth-note" role="status">${lx("目录暂未刷新，显示最近已上架的应用。", "Showing the last published catalog. Live refresh is temporarily unavailable.")}</p>` : ""}
    <div class="apps-grid">
      <section class="app-list" aria-label="${lx("应用列表", "App list")}">
        ${apps.map(app => `<button class="app-row ${app.app_id === selected.app_id ? "active" : ""}" data-app-id="${esc(app.app_id)}">${appIdentityIcon(app)}<span><strong>${esc(app.display_name)}</strong><small>${esc(kindLabel(app.kind))}${app.publisher ? ` · ${esc(app.publisher)}` : ""}</small></span>${publicationBadge(app)}</button>`).join("")}
      </section>
      <article class="app-detail">
        <div class="app-identity">${appIdentityIcon(selected, "large")}<div><div class="identity-line"><h2>${esc(selected.display_name)}</h2>${badge(selected.verification_state)}${publicationBadge(selected)}</div><p>${esc(selected.app_id)} · ${esc(selected.version)}${selected.publisher ? ` · ${esc(selected.publisher)}` : ""}</p></div></div>
        <p class="app-summary">${esc(selected.summary)}</p>
        <details class="technical-details"><summary>${t("details_label")}</summary>
        <div class="facts">
          <div><span>${t("page_apps_type")}</span><strong>${esc(kindLabel(selected.kind))}</strong></div>
          <div><span>${t("page_apps_format")}</span><strong>${t("page_apps_format_value")}</strong></div>
          <div><span>${t("page_apps_compat")}</span><strong>${browserRuntimeMissing ? lx("网页运行包尚未准备好", "Browser runtime is not ready") : `Client ${model.locale === "en-US" ? "auto-selects host" : "自动选择"} · ${model.locale === "en-US" ? "responsive" : "响应式"}`}</strong></div>
          <div><span>${t("page_apps_status")}</span><strong>${clientRequired ? lx("需桌面客户端", "Desktop client required") : previewOnly ? (model.locale === "en-US" ? "Private preview" : "私有隔离预览") : installed ? (model.locale === "en-US" ? "Installed and enabled" : "已安装并启用") : installedDisabled ? (model.locale === "en-US" ? "Installed, disabled" : "已安装，尚未启用") : cachedForWeb ? (model.locale === "en-US" ? "Verified package cached in this browser" : "已在本浏览器缓存并验真") : (model.locale === "en-US" ? "Verified candidate, not installed" : "已验收候选，尚未安装")}</strong></div>
          <div><span>${t("page_apps_service")}</span><strong>${serviceLike && browserRuntimeMissing ? lx("请在桌面客户端管理后台服务", "Manage background services in the desktop client") : serviceLike ? lx(`${serviceEntrypoints.length} 个入口由独立 daemon 托管`, `${serviceEntrypoints.length} entrypoints hosted by a separate daemon`) : t("page_apps_no_service")}</strong></div>
        </div>
        <div class="capabilities"><span>${t("page_apps_capabilities")}</span><div>${(selected.permissions || []).map(permission => `<code>${esc(permission)}</code>`).join("") || (model.locale === "en-US" ? "None" : "无")}</div></div>
        <div class="verification-note ${esc(selected.verification_state)}">${esc(selected.verification_summary)}</div>
        <div class="digest">component / package · ${esc(selected.component_sha256 || selected.package_digest_sha256)}</div>
        ${updateTransaction ? `<section class="binding-card"><span>${model.locale === "en-US" ? "Latest update transaction" : "最近一次更新事务"}</span><p><code>${esc(updateTransaction.from_version || "?")}</code> → <code>${esc(updateTransaction.to_version || "?")}</code> · ${esc(updateState.status || updateTransaction.status || "unknown")}</p><p>${model.locale === "en-US" ? "Migration" : "状态迁移"} <code>${esc(updateTransaction.migration?.status || "unknown")}</code> · ${model.locale === "en-US" ? "activation health" : "启用健康检查"} <code>${esc(updateTransaction.activation_health?.status || "unknown")}</code></p>${updateTransaction.rollback_reason ? `<p class="truth-note">${model.locale === "en-US" ? "Rolled back safely" : "已安全回滚"}：${esc(updateTransaction.rollback_reason)}</p>` : ""}</section>` : ""}
        </details>
        <div class="app-actions">
          ${desktopHandoff ? `<a class="primary-button" href="${esc(desktopAppUrl(selected.app_id))}" target="_blank" rel="noopener" data-open-desktop-app="${esc(selected.app_id)}">${icon("play")}${lx("在客户端打开", "Open in VibApp")}</a>` : ""}
          ${candidate ? `<button class="primary-button" data-install-app="${esc(selected.app_id)}" data-package-digest="${esc(selected.package_digest_sha256)}">${icon("box")}${selected.web_cache_eligible ? lx("下载并校验", "Download and verify") : lx("私有安装", "Install privately")}</button>` : ""}
          ${installedDisabled ? `<button class="primary-button" data-app-action="enable" data-action-app="${esc(selected.app_id)}">${icon("check")}${model.locale === "en-US" ? "Enable app" : "启用应用"}</button>` : ""}
          ${(installed || installedDisabled) && availableUpdate ? `<button class="primary-button" data-app-action="update" data-action-app="${esc(selected.app_id)}" data-package-digest="${esc(availableUpdate.package_digest_sha256)}">${icon("arrow")}${model.locale === "en-US" ? `Update to ${esc(availableUpdate.version)}` : `更新到 ${esc(availableUpdate.version)}`}</button>` : ""}
          ${launchable ? `<button class="primary-button" data-launch="${esc(selected.app_id)}">${icon("play")}${previewOnly ? t("page_apps_open_preview") : t("page_apps_open")}</button>` : ""}
          ${installed ? `<button class="secondary-button" data-app-action="disable" data-action-app="${esc(selected.app_id)}">${model.locale === "en-US" ? "Disable" : "停用"}</button>` : ""}
          ${installed || installedDisabled ? `<button class="secondary-button" data-app-action="status" data-action-app="${esc(selected.app_id)}">${model.locale === "en-US" ? "Refresh status" : "刷新状态"}</button><button class="secondary-button" data-app-action="uninstall" data-action-app="${esc(selected.app_id)}" data-disposition="retain">${model.locale === "en-US" ? "Uninstall, retain data" : "卸载并保留数据"}</button>` : ""}
          ${serviceLike && !installed && !installedDisabled && !candidate ? `<button class="secondary-button" disabled title="${t("page_apps_daemon_pending")}">${model.locale === "en-US" ? "Service unavailable" : "服务尚不可用"}</button>` : ""}
          ${!launchable && !serviceLike && !desktopHandoff ? `<button class="secondary-button" disabled>${t("page_apps_unavailable")}</button>` : ""}
          ${storePackageLinks(selected)}
        </div>
        ${desktopHandoff ? `<p class="truth-note">${lx("由 VibApp 客户端安装并运行，无需手动下载应用包。", "VibApp installs and runs this app. No manual package download needed.")}</p><details class="technical-details" ${model.desktopHandoffAppId === selected.app_id ? "open" : ""}><summary>${lx("没有打开？安装或更新客户端", "Didn't open? Install or update VibApp")}</summary><p>${lx("请在浏览器提示中允许打开 VibApp。还没安装？先下载客户端，安装并打开一次，再回来重试。", "Allow your browser to open VibApp. Not installed yet? Download the client, install and open it once, then return and try again.")}</p><button class="secondary-button" data-client-download aria-haspopup="dialog" aria-controls="client-download-dialog">${icon("download")}${lx("下载客户端", "Get VibApp")}</button></details>` : ""}
        ${browserRuntimeMissing && !cachedForWeb && !desktopHandoff ? `<p class="truth-note" role="status">${lx("这个版本尚无可验证的网页运行包，请使用桌面客户端。", "This version has no verified browser runtime. Use the desktop client.")}</p>` : ""}
        ${cachedForWeb ? `<p class="truth-note">${selected.web_runtime_available ? lx("公开包已通过 P2P 下载并逐文件校验；运行仍使用单独验证的同源 Web Runtime，不直接执行缓存字节。", "The public package was fetched over P2P and verified file by file. Execution still uses the separately verified same-origin Web Runtime; cached bytes are not executed directly.") : lx("公开包已通过 P2P 下载并逐文件校验，但它当前没有已验证的 Web Runtime；请使用 VibApp Client 运行。", "The public package was fetched over P2P and verified file by file, but it has no verified Web Runtime; use VibApp Client to run it.")}</p>` : ""}
        ${serviceEntrypoints.length ? `<section class="service-controls" aria-label="${lx("后台服务入口", "Background service entrypoints")}">${serviceEntrypoints.map(entrypoint => {
          const entrypointId = entrypoint.id;
          const running = (selected.active_service_entrypoints || []).includes(entrypointId);
          const status = selected.service_statuses?.[entrypointId] || {};
          return `<article class="service-control-card" data-service-entrypoint="${esc(entrypointId)}"><div><strong>${esc(entrypoint.label || entrypointId)}</strong><code>${esc(entrypointId)}</code><small>${lx("状态", "State")}: ${esc(status.state || (running ? "running" : "stopped"))}${status.health ? ` · ${lx("健康", "health")}: ${esc(status.health)}` : ""}</small></div><div>${installed ? `<button class="secondary-button" data-app-action="${running ? "service-stop" : "service-start"}" data-action-app="${esc(selected.app_id)}" data-entrypoint="${esc(entrypointId)}">${running ? lx("停止服务", "Stop service") : lx("启动后台服务", "Start service")}</button>${running ? `<button class="secondary-button" data-app-action="service-health" data-action-app="${esc(selected.app_id)}" data-entrypoint="${esc(entrypointId)}">${lx("检查健康状态", "Check health")}</button>` : ""}` : `<button class="secondary-button" disabled>${lx("服务尚不可用", "Service unavailable")}</button>`}</div></article>`;
        }).join("")}</section>` : ""}
        ${installed && serviceEntrypoints.length ? `<p class="truth-note">${lx("daemon 会在受限的独立进程中执行这些 Rust/Wasm 服务。关闭界面不会停止后台服务；停用或卸载才会停止。", "The daemon executes each Rust/Wasm service in a bounded separate process. Closing its UI does not stop services; disable or uninstall does.")}${selected.guest_execution_performed ? lx(" 已观察到本次安装的真实 guest 执行。", " Guest execution has been observed for this installation.") : ""}</p>` : ""}
      </article>
    </div>
  </div>`;
}

function renderBuilds() {
  const jobs = model.data.jobs || [];
  return `<div class="tool-page">
    <div class="page-heading"><div><p class="overline">${t("page_builds_overline")}</p><h1>${t("page_builds_title")}</h1><p>${t("page_builds_desc")}</p></div></div>
    <div class="job-list">
      ${jobs.length ? jobs.map(job => `<article class="job-card" data-job-card="${esc(job.task_id || job.job_id)}" data-task-kind="${esc(job.task_kind || "development")}">
        <div class="job-top"><div><h2>${esc(job.title)}</h2><p>${esc(job.job_id)} · ${dateLabel(job.updated_at_utc)}</p></div>${badge(job.verification?.status)}</div>
        <progress class="progress" max="100" value="${Math.max(0, Math.min(100, Number(job.progress_percent) || 0))}" aria-label="${esc(job.title)}"></progress>
        <div class="stage-row">${(job.stages || []).map(stage => `<span class="stage ${esc(stage.status)}">${stage.status === "succeeded" ? icon("check") : icon("clock")}${esc(stage.label)}</span>`).join("")}</div>
        <p class="job-note">${esc(job.verification?.summary || t("page_builds_note_waiting"))}</p>
        ${renderCodeagentDiagnostics(job)}
        <div class="job-actions"><button class="secondary-button" data-open-job-chat="${esc(job.task_id || job.job_id)}">${model.locale === "en-US" ? "Request chat and details" : "查看需求对话与详情"}</button></div>
      </article>`).join("") : `<div class="empty-state">${t("page_builds_empty")}</div>`}
    </div>
  </div>`;
}

function runtimeVariant(kind, name) {
  const kebab = name.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`);
  return kind?.[name] || kind?.[name[0].toUpperCase() + name.slice(1)] || kind?.[kebab];
}

function runtimeFieldValue(value) {
  if (!value || typeof value !== "object") return "";
  for (const key of ["text", "integer", "decimal", "date", "time", "timeZone", "time-zone", "choice", "secretHandle", "secret-handle", "Text", "Integer", "Decimal", "Date", "Time", "TimeZone", "Choice", "SecretHandle"]) {
    if (value[key] !== undefined && value[key] !== null) return typeof value[key] === "object" ? JSON.stringify(value[key]) : String(value[key]);
  }
  if (value.tag && Object.prototype.hasOwnProperty.call(value, "value")) return typeof value.value === "object" ? JSON.stringify(value.value) : String(value.value);
  if (value.tag && Object.prototype.hasOwnProperty.call(value, "val")) return typeof value.val === "object" ? JSON.stringify(value.val) : String(value.val);
  return "";
}

function runtimeInteractive() {
  const binding = model.runningApp?.runtime_binding;
  return Boolean((model.runningApp?.launcher_context?.installed || model.runningApp?.launcher_context?.foreground_interactive) && binding
    && ["entrypoint", "package_digest_sha256", "component_sha256", "session", "surface", "route"]
      .every(key => typeof binding[key] === "string" && binding[key].length)
    && typeof binding.generation === "string" && binding.generation.length);
}

function runtimeFieldTag(field) {
  return String(field.kind || "text")
    .replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`)
    .replace(/^-/, "")
    .toLowerCase();
}

function runtimeSecretHandle(field) {
  if (!field?.sensitive) return null;
  const value = field.value || {};
  return value["secret-handle"] ?? value.secretHandle ?? value.SecretHandle
    ?? (value.tag === "secret-handle" ? value.value : null);
}

function runtimeDisplayFieldValue(field) {
  const tag = runtimeFieldTag(field);
  const value = field.value || {};
  const payload = value[tag] ?? value[tag.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())]
    ?? (value.tag === tag ? value.value : null);
  if (tag === "date" && payload && typeof payload === "object") {
    return `${String(payload.year).padStart(4, "0")}-${String(payload.month).padStart(2, "0")}-${String(payload.day).padStart(2, "0")}`;
  }
  if (tag === "time" && payload && typeof payload === "object") {
    return `${String(payload.hour).padStart(2, "0")}:${String(payload.minute).padStart(2, "0")}:${String(payload.second || 0).padStart(2, "0")}`;
  }
  return runtimeFieldValue(value);
}

function renderRuntimeNode(node, childrenByParent, interactive, seen = new Set()) {
  if (!node || seen.has(node.id)) return "";
  seen.add(node.id);
  const kind = node.kind || {};
  const text = runtimeVariant(kind, "text");
  const button = runtimeVariant(kind, "button");
  const field = runtimeVariant(kind, "field");
  const list = runtimeVariant(kind, "listContainer");
  const progress = runtimeVariant(kind, "progress");
  const confirmation = runtimeVariant(kind, "confirmation");
  const children = (childrenByParent.get(node.id) || []).map(child => renderRuntimeNode(child, childrenByParent, interactive, seen)).join("");
  if (text) {
    const style = String(text.style || "body").toLowerCase();
    const tag = ["title", "heading"].includes(style) ? "h2" : "p";
    return `<${tag} class="runtime-text ${esc(style)}">${esc(text.text)}</${tag}>`;
  }
  if (button) {
    const style = String(button.style || "primary").toLowerCase();
    const disabled = Boolean(button.disabled || !interactive || model.runtimeDispatching);
    return `<button class="runtime-button ${esc(style)}" type="button" data-runtime-action="${esc(button.action)}" ${disabled ? "disabled" : ""} aria-disabled="${disabled}" title="${!interactive ? lx("隔离预览仅展示首屏；安装并启用后才可交互", "Isolated preview is read-only; install and enable the app to interact") : ""}">${esc(button.label)}</button>`;
  }
  if (field) {
    const fieldKind = runtimeFieldTag(field);
    const storedValue = runtimeDisplayFieldValue(field);
    const value = Object.prototype.hasOwnProperty.call(model.runtimeDraft, field.field) ? model.runtimeDraft[field.field] : storedValue;
    const choices = Array.isArray(field.choices) ? field.choices : [];
    const controlId = `runtime-field-${String(node.id).replace(/[^A-Za-z0-9_-]/g, "-")}`;
    const secretHandle = runtimeSecretHandle(field);
    const unavailableSecret = Boolean(field.sensitive && !secretHandle);
    const disabled = !interactive || model.runtimeDispatching || unavailableSecret;
    const control = fieldKind === "choice"
      ? `<select id="${esc(controlId)}" data-runtime-field="${esc(field.field)}" data-runtime-kind="choice" ${field.required ? "required" : ""} ${disabled ? "disabled" : ""}>${choices.map(item => `<option value="${esc(item.value)}" ${String(item.value) === value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select>`
      : fieldKind === "boolean"
        ? `<input id="${esc(controlId)}" data-runtime-field="${esc(field.field)}" data-runtime-kind="boolean" type="checkbox" ${typeof value === "boolean" ? value ? "checked" : "" : runtimeVariant(field.value, "boolean") || field.value?.tag === "boolean" && field.value.value ? "checked" : ""} ${disabled ? "disabled" : ""}>`
        : unavailableSecret
          ? `<input id="${esc(controlId)}" type="password" disabled aria-describedby="${esc(controlId)}-secret" value=""><small id="${esc(controlId)}-secret">${lx("敏感字段需要宿主 Secret Store 提供句柄；当前能力不可用，不能提交明文。", "This sensitive field requires a host Secret Store handle. The capability is unavailable, so plaintext cannot be submitted.")}</small>`
          : field.sensitive
            ? `<input id="${esc(controlId)}" data-runtime-field="${esc(field.field)}" data-runtime-kind="secret-handle" type="password" value="••••••••" readonly ${disabled ? "disabled" : ""} aria-label="${esc(field.label)} · ${lx("已由宿主安全保存", "stored securely by host")}">`
            : fieldKind === "text"
              ? `<textarea id="${esc(controlId)}" data-runtime-field="${esc(field.field)}" data-runtime-kind="text" rows="${Math.min(8, Math.max(1, String(value).split("\n").length))}" maxlength="4096" ${field.required ? "required" : ""} ${disabled ? "disabled" : ""}>\n${esc(value)}</textarea>`
            : `<input id="${esc(controlId)}" data-runtime-field="${esc(field.field)}" data-runtime-kind="${esc(fieldKind)}" type="${fieldKind === "date" ? "date" : fieldKind === "time" ? "time" : "text"}" ${["integer", "decimal"].includes(fieldKind) ? `inputmode="${fieldKind === "integer" ? "numeric" : "decimal"}"` : ""} maxlength="4096" value="${esc(value)}" ${field.required ? "required" : ""} ${disabled ? "disabled" : ""}>`;
    const validation = field.validationMessage || field.validation_message;
    return `<label class="runtime-field" for="${esc(controlId)}"><span>${esc(field.label)}${field.required ? ` · ${lx("必填", "Required")}` : ""}</span>${control}${validation ? `<small>${esc(validation)}</small>` : ""}</label>`;
  }
  if (list) {
    // Layout belongs to the host: keep source/keyboard order, but do not turn
    // semantic keypad rows and mode controls into a one-button-wide column.
    const directChildren = childrenByParent.get(node.id) || [];
    const actions = directChildren.filter(child => runtimeVariant(child.kind || {}, "button"));
    const actionLayout = actions.length > 1
      ? ` runtime-actions runtime-actions-${Math.min(4, actions.length)}` : "";
    return `<section class="runtime-group${actionLayout}" ${list.label ? `aria-label="${esc(list.label)}"` : ""}>${list.label ? `<h3>${esc(list.label)}</h3>` : ""}${children}</section>`;
  }
  if (progress) {
    const total = Number(progress.total) > 0 ? Number(progress.total) : 100;
    return `<div class="runtime-progress"><span>${esc(progress.label)}</span><progress value="${Math.max(0, Number(progress.current) || 0)}" max="${total}"></progress></div>`;
  }
  if (confirmation) {
    const disabled = !interactive || model.runtimeDispatching;
    return `<section class="runtime-confirmation" role="alertdialog" aria-label="${esc(confirmation.title)}"><strong>${esc(confirmation.title)}</strong><p>${esc(confirmation.message)}</p><div><button class="runtime-button secondary" type="button" data-runtime-action="${esc(confirmation.cancelAction || confirmation.cancel_action || confirmation["cancel-action"])}" ${disabled ? "disabled" : ""}>${lx("取消", "Cancel")}</button><button class="runtime-button ${confirmation.destructive ? "destructive" : "primary"}" type="button" data-runtime-action="${esc(confirmation.confirmAction || confirmation.confirm_action || confirmation["confirm-action"])}" ${disabled ? "disabled" : ""}>${lx("确认", "Confirm")}</button></div></section>`;
  }
  return "";
}

function renderRuntimeTree(nodes, rootId, interactive = runtimeInteractive()) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const childrenByParent = new Map();
  nodes.forEach(node => {
    if (node.parent == null) return;
    const children = childrenByParent.get(node.parent) || [];
    children.push(node);
    childrenByParent.set(node.parent, children);
  });
  const root = byId.get(rootId) || nodes.find(node => node.parent == null);
  return root ? renderRuntimeNode(root, childrenByParent, interactive) : "";
}

function renderCollaborationPanel() {
  const appId = model.runningApp?.descriptor?.id;
  if (!appId || !model.runningApp?.launcher_context?.installed) return "";
  const status = model.collaborationStatus || { state: "stopped", sessions: [] };
  const sessions = Array.isArray(status.sessions) ? status.sessions : [];
  const session = sessions.find(item => item.appId === appId)
    || (status.appId === appId ? sessions[0] : null);
  const enabled = model.networkSettings?.p2pEnabled === true && model.networkSettings?.rtcEnabled === true;
  const busy = model.collaborationBusy;
  const peerLabel = t("collaboration_peers").replace("{peers}", String(session?.peerCount || 0));
  const expiresLabel = session?.expiresAt
    ? t("collaboration_expires").replace("{time}", dateLabel(session.expiresAt))
    : "";
  const content = `
    <div class="collaboration-heading"><div><span class="overline">VIBAPP · RTC</span><h2 id="collaboration-panel-title">${t("collaboration_title")}</h2><p>${t("collaboration_desc")}</p></div><span class="collaboration-state ${session ? "active" : "local"}">${session ? lx("已共享", "Shared") : lx("仅本机", "Local only")}</span></div>
    ${model.collaborationError ? `<div class="runtime-error" role="alert"><p>${esc(model.collaborationError)}</p></div>` : ""}
    ${!enabled ? `<p class="collaboration-disabled">${t("collaboration_disabled")}</p>` : session ? `
      <div class="collaboration-session">
        <label><span>${t("collaboration_channel")}</span><input readonly value="${esc(session.channelId || "")}" aria-label="${t("collaboration_channel")}"></label>
        <div><strong>${esc(peerLabel)}</strong><small>${esc(expiresLabel)}</small></div>
        <button class="secondary-button" type="button" data-collaboration-leave="${esc(session.sessionId)}" ${busy ? "disabled" : ""}>${t("collaboration_leave")}</button>
      </div>` : `
      <label class="settings-toggle collaboration-confirm"><input type="checkbox" data-collaboration-confirm ${busy ? "disabled" : ""}> ${t("collaboration_confirm")}</label>
      <div class="collaboration-actions">
        <button class="primary-button" type="button" data-collaboration-create ${busy ? "disabled" : ""}>${t("collaboration_create")}</button>
        <div class="collaboration-join"><input data-collaboration-channel maxlength="36" placeholder="${t("collaboration_join_placeholder")}" ${busy ? "disabled" : ""}><button class="secondary-button" type="button" data-collaboration-join ${busy ? "disabled" : ""}>${t("collaboration_join")}</button></div>
      </div>`}
    <p class="settings-security-note">${t("collaboration_local_note")}</p>`;
  if (model.runtimeWindowAppId) {
    return `<details class="collaboration-panel compact-overlay">
      <summary><span class="runtime-light" aria-hidden="true"></span><span>${lx("协作", "Collaboration")}</span><span class="collaboration-state ${session ? "active" : "local"}">${session ? lx("已共享", "Shared") : lx("仅本机", "Local")}</span></summary>
      <div class="collaboration-overlay-body" aria-labelledby="collaboration-panel-title">${content}</div>
    </details>`;
  }
  return `<aside class="collaboration-panel" aria-labelledby="collaboration-panel-title">${content}</aside>`;
}

function runtimeTrustedIdentity(result) {
  const descriptor = result?.descriptor || {};
  const identity = result?.trusted_identity || {};
  const binding = result?.runtime_binding || {};
  const installed = result?.launcher_context?.installed === true;
  const exact = installed
    && identity.app_id === descriptor.id
    && identity.display_name === descriptor.display_name
    && identity.version === descriptor.version
    && identity.active_package_digest_sha256 === binding.package_digest_sha256
    && identity.generation === binding.generation;
  const permissionCount = Number.isSafeInteger(identity.permission_count) && identity.permission_count >= 0
    ? identity.permission_count
    : null;
  return {
    appId: exact ? String(identity.app_id) : "vibapp.binding-pending",
    displayName: exact ? String(identity.display_name) : lx("应用身份待确认", "Application identity pending"),
    version: exact ? String(identity.version) : "pending",
    publisher: exact ? String(identity.publisher || "VibApp") : null,
    publicationBadge: exact ? String(identity.publication_badge || "private") : "binding-pending",
    permissionCount: exact ? permissionCount : null,
    exact,
  };
}

function renderRuntimeTrustStrip(result) {
  const identity = runtimeTrustedIdentity(result);
  const trust = identity.exact ? lx("已验证 · 运行中", "Verified · Running") : lx("身份绑定待确认", "Identity binding pending");
  const origin = identity.publicationBadge === "public-appstore" ? lx("AppStore 公开", "Public AppStore") : lx("私有", "Private");
  const permissions = identity.permissionCount == null
    ? lx("权限状态不可用", "Permission status unavailable")
    : lx(`${identity.permissionCount} 项权限受宿主管理`, `${identity.permissionCount} permissions host-managed`);
  const label = lx(
    `${identity.displayName}，${trust}，${identity.publisher || "发布者待确认"}，${permissions}`,
    `${identity.displayName}, ${trust}, ${identity.publisher || "publisher pending"}, ${permissions}`,
  );
  return `<div class="runtime-trust-strip ${identity.exact ? "verified" : "pending"}" data-host-trusted-identity data-binding-exact="${identity.exact}" role="status" aria-label="${esc(label)}" title="${esc(label)}">
    <span class="runtime-trust-dot" aria-hidden="true"></span><strong>${esc(identity.displayName)}</strong><span>${trust}</span><span class="runtime-trust-publisher">${esc(identity.publisher || lx("发布者待确认", "Publisher pending"))}</span><span class="runtime-trust-origin">${origin}</span><span class="runtime-trust-permissions">${permissions}</span>
  </div>`;
}

function renderRuntime() {
  const result = model.runningApp;
  if (!result) return renderApps();
  const surface = result.surface || {};
  const nodes = surface.view?.nodes || [];
  const context = result.launcher_context || {};
  const runtimeIdentity = (model.data?.apps || []).find(app => app.app_id === result.descriptor?.id) || result.descriptor || {};
  const closeLabel = model.runtimeWindowAppId ? lx("关闭窗口", "Close window") : t("page_runtime_back");
  const guestSurfaceClass = runtimeSurfaceIsSingleDisplay(surface) ? " single-display" : "";
  const trusted = runtimeTrustedIdentity(result);
  const trustedWindowLabel = lx(
    `由 VibApp Client 验证并托管的应用窗口：${trusted.displayName}，${trusted.appId}，版本 ${trusted.version}`,
    `VibApp Client verified application window: ${trusted.displayName}, ${trusted.appId}, version ${trusted.version}`,
  );
  const trustedIdentity = model.runtimeWindowAppId
    ? renderRuntimeTrustStrip(result)
    : `<header>${appIdentityIcon(result.descriptor)}<div><strong>${esc(result.descriptor?.display_name)}</strong><small>${esc(result.descriptor?.id)} · ${esc(result.descriptor?.version)}</small></div><span class="runtime-identity-badges">${publicationBadge(runtimeIdentity)}<span class="compatibility-badge">${lx("Client 托管", "Client hosted")}</span></span></header>`;
  return `<div class="runtime-page${model.runtimeWindowAppId ? " standalone" : ""}">
    <div class="runtime-toolbar"><button class="back-button" data-close-runtime>${icon("back")}${closeLabel}</button><div><span class="runtime-light"></span>VibApp ${context.mode === "isolated-preview" ? t("page_runtime_mode_preview") : t("page_runtime_mode_running")} · <span data-layout-label>${t("page_runtime_layout_auto")}</span></div><code>${esc(result.runtime_binding?.component_sha256 || result.component_sha256 || result.browser_artifact_sha256 || "")}</code></div>
    <article class="runtime-window" aria-busy="${model.runtimeDispatching}" aria-label="${esc(trustedWindowLabel)}">
      ${trustedIdentity}
      ${model.runtimeError ? `<div class="runtime-error" role="alert"><strong>${lx("应用操作失败", "App action failed")}</strong><p>${esc(model.runtimeError)}</p><small>${lx("当前界面与输入已保留。若会话、路由或版本已过期，请关闭后重新打开应用。", "The current view and input remain visible. If the session, route, or generation is stale, close and reopen the app.")}</small></div>` : ""}
      <section class="guest-surface${guestSurfaceClass}" aria-label="${lx("生成应用界面", "Generated app interface")}">
        ${renderRuntimeTree(nodes, surface.view?.root)}
      </section>
      <footer>${icon("box")} ${context.mode === "isolated-preview" ? t("page_runtime_note") : lx("Rust Component 返回语义节点；Client 负责安全渲染、键盘交互和响应式布局。操作会携带精确的安装、会话、surface 与 route 绑定。", "Rust Component returns semantic nodes; Client owns secure rendering, keyboard interaction, and responsive layout. Actions carry exact installation, session, surface, and route bindings.")}</footer>
    </article>
    ${renderCollaborationPanel()}
  </div>`;
}

function runtimeFieldDefinitions() {
  return (model.runningApp?.surface?.view?.nodes || [])
    .map(node => runtimeVariant(node.kind || {}, "field"))
    .filter(Boolean);
}

function runtimeWireValue(field, control) {
  const tag = runtimeFieldTag(field);
  if (field.sensitive) {
    const handle = runtimeSecretHandle(field);
    if (!handle) throw new Error(lx("敏感字段缺少宿主 Secret Store 句柄，已拒绝提交。", "Sensitive field has no host Secret Store handle; submission was refused."));
    return { tag: "secret-handle", value: String(handle) };
  }
  if (tag === "boolean") return { tag, value: Boolean(control?.checked) };
  const raw = String(control?.value ?? "");
  if (!raw.length) {
    return { tag: "empty" };
  }
  if (tag === "integer") {
    if (!/^-?\d+$/.test(raw)) throw new Error(lx(`${field.label} 必须是整数。`, `${field.label} must be an integer.`));
    const parsed = Number(raw);
    if (!Number.isSafeInteger(parsed)) throw new Error(lx(`${field.label} 超出安全整数范围。`, `${field.label} exceeds the safe integer range.`));
    return { tag, value: parsed };
  }
  if (tag === "decimal") {
    if (!/^-?(?:\d+\.?\d*|\.\d+)$/.test(raw)) throw new Error(lx(`${field.label} 必须是十进制数。`, `${field.label} must be a decimal.`));
    return { tag, value: raw };
  }
  if (tag === "date") {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
    if (!match) throw new Error(lx(`${field.label} 必须是有效日期。`, `${field.label} must be a valid date.`));
    return { tag, value: { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) } };
  }
  if (tag === "time") {
    const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(raw);
    if (!match) throw new Error(lx(`${field.label} 必须是有效时间。`, `${field.label} must be a valid time.`));
    return { tag, value: { hour: Number(match[1]), minute: Number(match[2]), second: Number(match[3] || 0) } };
  }
  if (!['text', 'time-zone', 'choice'].includes(tag)) throw new Error(lx(`不支持字段类型 ${tag}。`, `Unsupported field kind ${tag}.`));
  return { tag, value: raw };
}

function collectRuntimeFields() {
  const controls = [...document.querySelectorAll("[data-runtime-field]")];
  const fields = runtimeFieldDefinitions().map(field => {
    const control = controls.find(item => item.dataset.runtimeField === field.field) || null;
    if (!control && !field.sensitive) throw new Error(lx(`找不到字段 ${field.field}。`, `Field ${field.field} is unavailable.`));
    const draftValue = runtimeFieldTag(field) === "boolean" ? Boolean(control?.checked) : String(control?.value ?? "");
    if (!field.sensitive) model.runtimeDraft[field.field] = draftValue;
    return { field: field.field, value: runtimeWireValue(field, control) };
  });
  return fields;
}

function acceptRuntimeActionResult(previous, next) {
  const expected = previous.runtime_binding || {};
  const actual = next?.runtime_binding || {};
  const keys = ["entrypoint", "package_digest_sha256", "component_sha256", "generation", "session", "surface", "route"];
  if (!keys.every(key => actual[key] === expected[key])) throw new Error(lx("运行时返回了不匹配或过期的绑定，已拒绝更新界面。", "Runtime returned a mismatched or stale binding; the UI update was refused."));
  const surface = next?.surface;
  if (!surface || surface.session !== expected.session || surface.surface !== expected.surface || surface.route !== expected.route || !surface.view) {
    throw new Error(lx("运行时返回了伪造或过期的 surface update，已拒绝更新界面。", "Runtime returned a forged or stale surface update; the UI update was refused."));
  }
  return { ...previous, ...next, descriptor: previous.descriptor, launcher_context: previous.launcher_context, runtime_binding: { ...expected, ...actual }, surface };
}

function runtimeEventId() {
  const uuid = window.crypto?.randomUUID?.();
  if (uuid) return `desktop-action-${uuid}`;
  return `desktop-action-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function runtimeSurfaceIsDisplayOnly(surface) {
  const nodes = Array.isArray(surface?.view?.nodes) ? surface.view.nodes : [];
  if (!nodes.length) return false;
  return nodes.every(node => {
    const kind = node?.kind || {};
    if (runtimeVariant(kind, "button") || runtimeVariant(kind, "field") || runtimeVariant(kind, "confirmation")) return false;
    return Boolean(runtimeVariant(kind, "text") || runtimeVariant(kind, "listContainer") || runtimeVariant(kind, "progress"));
  });
}

function runtimeSurfaceIsSingleDisplay(surface) {
  const nodes = Array.isArray(surface?.view?.nodes) ? surface.view.nodes : [];
  if (nodes.length !== 1 || nodes[0]?.parent != null) return false;
  const text = runtimeVariant(nodes[0]?.kind || {}, "text");
  if (!text) return false;
  const style = String(text.style || "body").toLowerCase();
  return style === "title" || style === "heading";
}

function stopRuntimeSurfaceRefresh(deactivate = true) {
  if (model.runtimeRefreshTimer != null) window.clearTimeout?.(model.runtimeRefreshTimer);
  model.runtimeRefreshTimer = null;
  if (deactivate) model.runtimeRefreshActive = false;
}

function runtimeSurfaceRefreshEligible() {
  return Boolean((model.runtimeWindowAppId || model.runningApp?.launcher_context?.foreground_interactive)
    && model.runtimeRefreshActive
    && model.route === "runtime"
    && model.runningApp
    && runtimeInteractive()
    && !model.runtimeDispatching
    && document.visibilityState !== "hidden"
    && (window.__TAURI__?.core?.invoke || window.VibAppWebBridge?.invoke));
}

function scheduleRuntimeSurfaceRefresh(delay = 1000) {
  if (model.runtimeRefreshTimer != null) window.clearTimeout?.(model.runtimeRefreshTimer);
  model.runtimeRefreshTimer = null;
  if (!runtimeSurfaceRefreshEligible() || model.runtimeRefreshInFlight) return;
  model.runtimeRefreshTimer = window.setTimeout(() => {
    model.runtimeRefreshTimer = null;
    void refreshRuntimeSurface();
  }, Math.max(1000, delay));
}

async function refreshRuntimeSurface() {
  if (!runtimeSurfaceRefreshEligible() || model.runtimeRefreshInFlight) return;
  const previous = model.runningApp;
  const binding = previous.runtime_binding;
  const previousSurface = JSON.stringify(previous.surface);
  model.runtimeRefreshInFlight = true;
  try {
    model.runtimeRefreshRequest = call("refresh_app_surface", {
      appId: previous.descriptor.id,
      entrypoint: binding.entrypoint,
      packageDigestSha256: binding.package_digest_sha256,
      componentSha256: binding.component_sha256,
      generation: binding.generation,
      session: binding.session,
      surface: binding.surface,
      route: binding.route,
      eventId: runtimeEventId().replace("desktop-action-", "desktop-refresh-"),
    });
    const next = await model.runtimeRefreshRequest;
    if (model.runningApp !== previous) return;
    const accepted = acceptRuntimeActionResult(previous, next);
    const changed = JSON.stringify(accepted.surface) !== previousSurface;
    model.runningApp = accepted;
    model.runtimeError = null;
    // An unchanged tick is normal for clocks with minute precision, paused
    // stopwatches, and jobs waiting on input. It is not a stop instruction.
    if (changed) render({ background: true });
  } catch (failure) {
    if (model.runningApp === previous) {
      model.runtimeError = String(failure);
      model.runtimeRefreshActive = false;
      render();
    }
  } finally {
    model.runtimeRefreshInFlight = false;
    model.runtimeRefreshRequest = null;
    scheduleRuntimeSurfaceRefresh();
  }
}

async function dispatchRuntimeAction(action) {
  if (model.runtimeRefreshRequest) {
    const refreshingApp = model.runningApp?.descriptor?.id;
    try { await model.runtimeRefreshRequest; } catch (_) { return; }
    if (model.runningApp?.descriptor?.id !== refreshingApp) return;
  }
  if (!runtimeInteractive() || model.runtimeDispatching || !action) return;
  const previous = model.runningApp;
  const binding = previous.runtime_binding;
  const eventId = runtimeEventId();
  let fields;
  try {
    fields = collectRuntimeFields();
  } catch (failure) {
    model.runtimeError = String(failure);
    render();
    return;
  }
  model.runtimeDispatching = true;
  model.runtimeError = null;
  document.querySelector(".runtime-window")?.setAttribute("aria-busy", "true");
  document.querySelectorAll("[data-runtime-action], [data-runtime-field]").forEach(control => { control.disabled = true; });
  try {
    const next = await call("dispatch_app_action", {
      appId: previous.descriptor.id,
      entrypoint: binding.entrypoint,
      packageDigestSha256: binding.package_digest_sha256,
      componentSha256: binding.component_sha256,
      generation: binding.generation,
      session: binding.session,
      surface: binding.surface,
      route: binding.route,
      action,
      eventId,
      fields,
    });
    model.runningApp = acceptRuntimeActionResult(previous, next);
    model.runtimeDraft = {};
  } catch (failure) {
    model.runtimeError = String(failure);
  } finally {
    model.runtimeDispatching = false;
    render();
    scheduleRuntimeSurfaceRefresh();
  }
}

function renderSettings() {
  const locales = [
    { value: "auto", label: t("settings_language_auto") },
    { value: "zh-CN", label: t("settings_language_zh") },
    { value: "en-US", label: t("settings_language_en") },
  ];
  const settings = model.modelSettings || {};
  const lockedModels = hostedServiceUnavailable() || settings.readOnly || !model.modelSettings;
  const lockedAgent = hostedServiceUnavailable() || model.codeAgentSettings?.readOnly || !model.codeAgentSettings;
  const lockedNetwork = hostedServiceUnavailable() || model.networkSettings?.readOnly || !model.networkSettings;
  const generation = settings.generation || {
    enabled: false,
    baseUrl: "",
    model: "",
    protocol: "chat-completions",
    timeoutSeconds: 45,
    maxOutputTokens: 512,
    temperature: 0,
    hasApiKey: false,
  };
  const embedding = settings.embedding || {
    enabled: false,
    baseUrl: "",
    model: "",
    dimensions: 384,
    timeoutSeconds: 5,
    hasApiKey: false,
  };
  const secretHint = value => value ? t("settings_api_key_saved") : t("settings_api_key_empty");
  const codeAgent = model.codeAgentSettings || {
    selectedProvider: "",
    modelByProvider: {},
    providers: [],
  };
  const selectedCodeAgent = codeAgent.providers.find(item => item.id === codeAgent.selectedProvider)
    || codeAgent.providers[0];
  const selectedCodeAgentConfigurable = Boolean(selectedCodeAgent?.preferenceConfigurable)
    || Boolean(selectedCodeAgent?.executionSupported && selectedCodeAgent?.installed);
  const codeAgentStatus = provider => provider.status === "local-live-containment-unavailable"
    ? t("settings_codeagent_pending")
    : !provider.executionSupported
      ? t("settings_codeagent_pending")
    : provider.installed
      ? t("settings_codeagent_ready")
      : t("settings_codeagent_missing");
  const network = model.networkSettings || {
    p2pEnabled: false,
    seedVerifiedApps: false,
    allowUserFileSeeding: false,
    uploadLimitKibPerSecond: 1024,
    downloadLimitKibPerSecond: 4096,
    cacheLimitMib: 2048,
    maxActiveTransfers: 8,
    rtcEnabled: false,
    maxActiveChannels: 8,
    turnEnabled: false,
    turnUrls: [],
    turnUsername: "",
    hasTurnCredential: false,
  };
  const networkStatus = model.networkStatus || {
    state: "unknown",
    foregroundOnly: Boolean(window.VibAppWebBridge?.invoke),
    activeTransfers: 0,
    torrents: 0,
    lastError: null,
  };
  const networkStateKey = {
    running: "settings_network_status_running",
    stopped: "settings_network_status_stopped",
    unavailable: "settings_network_status_unavailable",
    error: "settings_network_status_error",
    starting: "settings_network_status_starting",
    unknown: "settings_network_status_unknown",
  }[networkStatus.state] || "settings_network_status_unknown";
  const transferSummary = t("settings_network_transfer_summary")
    .replace("{transfers}", String(networkStatus.activeTransfers || 0))
    .replace("{torrents}", String(networkStatus.torrents || 0));

  return `<div class="tool-page settings-page">
    <div class="page-heading"><div><h1>${t("settings_title")}</h1><p>${t("settings_desc")}</p></div></div>
    ${serviceNotice()}
    <section class="settings-shell">
      <div class="settings-section">
        <h3>${t("settings_section_gui")}</h3>
        <label class="setting-row">
          <span>${t("settings_gui_locale")}</span>
          <select class="setting-select" data-setting-locale>
            ${locales.map(item => `<option value="${item.value}" ${model.localePreference === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
          </select>
        </label>
        <p class="settings-helper">${t("settings_gui_locale_desc")}</p>
      </div>
      <details class="settings-disclosure"><summary><span><strong>${t("settings_network")}</strong><small>${t("settings_network_desc")}</small></span></summary>
      <form class="settings-section network-settings-form">
        ${lockedNetwork ? `<p class="settings-readonly">${t("service_label")}</p>` : ""}
        <fieldset class="settings-fields" ${lockedNetwork ? "disabled" : ""}>
        <div class="network-runtime-state ${esc(networkStatus.state)}" role="status">
          <span class="network-runtime-light" aria-hidden="true"></span>
          <div><strong>${t("settings_network_status")} · ${t(networkStateKey)}</strong><small>${networkStatus.foregroundOnly ? t("settings_network_foreground") : t("settings_network_background")}</small><small>${esc(transferSummary)}</small>${networkStatus.lastError ? `<small class="network-runtime-error">${esc(networkStatus.lastError)}</small>` : ""}</div>
          <button class="secondary-button" type="button" data-network-node-action="${networkStatus.state === "running" ? "stop" : "start"}" ${model.busy || networkStatus.state !== "running" && !network.p2pEnabled ? "disabled" : ""} title="${networkStatus.state !== "running" && !network.p2pEnabled ? esc(t("settings_network_enable_first")) : ""}">${networkStatus.state === "running" ? t("settings_network_stop") : t("settings_network_start")}</button>
        </div>
        <label class="settings-toggle"><input type="checkbox" name="p2p_enabled" ${network.p2pEnabled ? "checked" : ""}> ${t("settings_network_enabled")}</label>
        <label class="settings-toggle"><input type="checkbox" name="seed_verified_apps" ${network.seedVerifiedApps ? "checked" : ""}> ${t("settings_network_seed_apps")}</label>
        <label class="settings-toggle"><input type="checkbox" name="allow_user_file_seeding" ${network.allowUserFileSeeding ? "checked" : ""}> ${t("settings_network_seed_files")}</label>
        <div class="settings-grid">
          <label><span>${t("settings_network_upload")}</span><input name="upload_limit" type="number" min="0" max="1048576" required value="${esc(network.uploadLimitKibPerSecond)}"></label>
          <label><span>${t("settings_network_download")}</span><input name="download_limit" type="number" min="0" max="1048576" required value="${esc(network.downloadLimitKibPerSecond)}"></label>
          <label><span>${t("settings_network_cache")}</span><input name="cache_limit" type="number" min="128" max="1048576" required value="${esc(network.cacheLimitMib)}"></label>
          <label><span>${t("settings_network_transfers")}</span><input name="max_transfers" type="number" min="1" max="64" required value="${esc(network.maxActiveTransfers)}"></label>
        </div>
        <label class="settings-toggle"><input type="checkbox" name="rtc_enabled" ${network.rtcEnabled ? "checked" : ""}> ${t("settings_network_rtc")}</label>
        <div class="settings-grid">
          <label><span>${t("settings_network_channels")}</span><input name="max_channels" type="number" min="1" max="32" required value="${esc(network.maxActiveChannels)}"></label>
        </div>
        <label class="settings-toggle"><input type="checkbox" name="turn_enabled" ${network.turnEnabled ? "checked" : ""}> ${t("settings_network_turn")}</label>
        <div class="settings-grid">
          <label><span>${t("settings_network_turn_urls")}</span><textarea name="turn_urls" rows="2" maxlength="1025" placeholder="turns:turn.example.com:5349?transport=tcp">${esc((network.turnUrls || []).join("\n"))}</textarea></label>
          <label><span>${t("settings_network_turn_username")}</span><input name="turn_username" maxlength="256" value="${esc(network.turnUsername || "")}"></label>
          <label><span>${t("settings_network_turn_credential")}</span><input name="turn_credential" type="password" maxlength="512" autocomplete="new-password" placeholder="${esc(network.hasTurnCredential ? t("settings_network_turn_saved") : t("settings_network_turn_empty"))}"></label>
        </div>
        ${network.hasTurnCredential ? `<label class="settings-clear"><input type="checkbox" name="clear_turn_credential"> ${t("settings_network_turn_clear")}</label>` : ""}
        <p class="settings-security-note">${lx("P2P 文件传输和 RTC 基于 RoomHash 网络。", "P2P file transfer and RTC are based on the RoomHash network.")} ${t("settings_network_note")}</p>
        <button class="primary-button" type="submit" data-save-network-settings>${t("settings_network_save")}</button>
        </fieldset>
      </form></details>
      <details class="settings-disclosure"><summary><span><strong>${t("settings_codeagent")}</strong><small>${t("settings_codeagent_desc")}</small></span></summary>
      <form class="settings-section codeagent-settings-form">
        ${lockedAgent ? `<p class="settings-readonly">${t("service_label")}</p>` : ""}
        <fieldset class="settings-fields" ${lockedAgent ? "disabled" : ""}>
        <div class="settings-grid">
          <label><span>${t("settings_codeagent_provider")}</span><select name="selected_provider" data-codeagent-provider>
            ${codeAgent.providers.map(provider => `<option value="${esc(provider.id)}" ${provider.id === selectedCodeAgent?.id ? "selected" : ""}>${esc(provider.displayName)}</option>`).join("")}
          </select></label>
          <label><span>${t("settings_codeagent_model")}</span><input name="codeagent_model" maxlength="256" required placeholder="${esc(t("settings_codeagent_model_hint"))}" value="${esc(codeAgent.modelByProvider?.[selectedCodeAgent?.id] || "")}"></label>
        </div>
        ${selectedCodeAgent ? `<p class="codeagent-provider-state ${selectedCodeAgent.executionSupported && selectedCodeAgent.installed ? "ready" : "pending"}"><strong>${esc(selectedCodeAgent.displayName)}</strong> · ${esc(codeAgentStatus(selectedCodeAgent))}</p>` : ""}
        <p class="settings-security-note">${t("settings_codeagent_source_only")}</p>
        <button class="primary-button" type="submit" data-save-codeagent-settings ${selectedCodeAgentConfigurable ? "" : "disabled"}>${t("settings_codeagent_save")}</button>
        </fieldset>
      </form></details>
      <details class="settings-disclosure"><summary><span><strong>${t("settings_byom")}</strong><small>${t("settings_byom_desc")}</small></span></summary>
      <form class="settings-section model-settings-form">
        ${lockedModels ? `<p class="settings-readonly">${t("service_label")}</p>` : ""}
        <fieldset class="settings-fields" ${lockedModels ? "disabled" : ""}>
        <fieldset class="model-provider-card">
          <legend>${t("settings_generation")}</legend>
          <p>${t("settings_generation_desc")}</p>
          <label class="settings-toggle"><input type="checkbox" name="generation_enabled" ${generation.enabled ? "checked" : ""}> ${t("settings_enabled")}</label>
          <div class="settings-grid">
            <label><span>${t("settings_base_url")}</span><input name="generation_base_url" type="url" maxlength="512" required value="${esc(generation.baseUrl)}"></label>
            <label><span>${t("settings_model")}</span><input name="generation_model" maxlength="256" required value="${esc(generation.model)}"></label>
            <label><span>${t("settings_protocol")}</span><select name="generation_protocol"><option value="chat-completions" ${generation.protocol === "chat-completions" ? "selected" : ""}>${t("settings_protocol_chat")}</option><option value="responses" ${generation.protocol === "responses" ? "selected" : ""}>${t("settings_protocol_responses")}</option></select></label>
            <label><span>${t("settings_api_key")}</span><input name="generation_api_key" type="password" maxlength="8192" autocomplete="new-password" placeholder="${esc(secretHint(generation.hasApiKey))}"></label>
            <label><span>${t("settings_timeout")}</span><input name="generation_timeout" type="number" min="5" max="120" required value="${esc(generation.timeoutSeconds)}"></label>
            <label><span>${t("settings_max_tokens")}</span><input name="generation_max_tokens" type="number" min="128" max="4096" required value="${esc(generation.maxOutputTokens)}"></label>
            <label><span>${t("settings_temperature")}</span><input name="generation_temperature" type="number" min="0" max="2" step="0.1" required value="${esc(generation.temperature)}"></label>
          </div>
          ${generation.hasApiKey ? `<label class="settings-clear"><input type="checkbox" name="generation_clear_api_key"> ${t("settings_clear_api_key")}</label>` : ""}
        </fieldset>
        <fieldset class="model-provider-card">
          <legend>${t("settings_embedding")}</legend>
          <p>${t("settings_embedding_desc")}</p>
          <label class="settings-toggle"><input type="checkbox" name="embedding_enabled" ${embedding.enabled ? "checked" : ""}> ${t("settings_enabled")}</label>
          <div class="settings-grid">
            <label><span>${t("settings_base_url")}</span><input name="embedding_base_url" type="url" maxlength="512" required value="${esc(embedding.baseUrl)}"></label>
            <label><span>${t("settings_model")}</span><input name="embedding_model" maxlength="256" required value="${esc(embedding.model)}"></label>
            <label><span>${t("settings_dimensions")}</span><input name="embedding_dimensions" type="number" min="1" max="8192" required value="${esc(embedding.dimensions)}"></label>
            <label><span>${t("settings_timeout")}</span><input name="embedding_timeout" type="number" min="1" max="30" required value="${esc(embedding.timeoutSeconds)}"></label>
            <label><span>${t("settings_api_key")}</span><input name="embedding_api_key" type="password" maxlength="8192" autocomplete="new-password" placeholder="${esc(secretHint(embedding.hasApiKey))}"></label>
          </div>
          ${embedding.hasApiKey ? `<label class="settings-clear"><input type="checkbox" name="embedding_clear_api_key"> ${t("settings_clear_api_key")}</label>` : ""}
        </fieldset>
        <p class="settings-security-note">${t("settings_secure_note")}</p>
        <button class="primary-button" type="submit" data-save-model-settings>${t("settings_save")}</button>
        </fieldset>
      </form></details>
      <details class="settings-disclosure"><summary><span><strong>${t("settings_help")}</strong></span></summary><div class="settings-section"><p>${t("settings_codeagent_excluded_desc")}</p><p>${t("settings_secure_note")}</p></div></details>
    </section>
  </div>`;
}

function hasUnsavedFormEdits() {
  return [...document.querySelectorAll("#view input, #view textarea, #view select, #view [contenteditable]")].some(control => {
    if (control === document.activeElement) return true;
    if (control.isContentEditable) return true;
    if (control.tagName === "SELECT") return [...control.options].some(option => option.selected !== option.defaultSelected);
    if (["checkbox", "radio"].includes(control.type)) return control.checked !== control.defaultChecked;
    return control.value !== control.defaultValue;
  });
}

function renderStoreOpenRequest() {
  const request = model.storeOpenRequest;
  if (!request || !model.installWindow || model.runtimeWindowAppId) return "";
  const record = request.record || {};
  const ready = request.status === "ready";
  const failed = request.status === "error";
  const labels = { clock: lx("读取时间", "Read the time"), kv: lx("保存此应用的数据", "Save this app's data"),
    log: lx("记录运行日志", "Write diagnostic logs"), "host-info": lx("读取运行环境信息", "Read runtime information"), settings: lx("读取应用设置", "Read app settings") };
  const permissions = (record.runtime?.capabilities || []).map(c => {
    const name = String(c.interface || "").replace("vibapp:experimental-v0/", "").replace("@0.0.1", "");
    return labels[name] || name;
  });
  return `<section class="store-open-request" aria-labelledby="install-title">
    <div class="install-heading">${appIdentityIcon({ app_id: request.app_id, display_name: record.app?.display_name || "VibApp" }, "large")}
      <div><p class="install-eyebrow">${lx("安装应用", "Install app")}</p><h1 id="install-title">${esc(record.app?.display_name || request.app_id)}</h1>
      ${record.app ? `<p class="install-publisher">${esc(record.app.publisher?.display_name)} · ${esc(record.app.version)}</p>` : ""}</div></div>
    ${ready ? `<p class="install-question">${lx("要在这台设备上安装并打开吗？", "Install and open on this device?")}</p>
      <div class="install-permissions"><h2>${lx("此应用需要", "This app needs to")}</h2><ul>${permissions.map(p => `<li>${esc(p)}</li>`).join("") || `<li>${lx("无需额外权限", "No additional permissions")}</li>`}</ul></div>
      <details class="technical-details"><summary>${lx("应用与权限详情", "App and permission details")}</summary><p>${esc(record.app?.description)}</p><p>${esc(record.digests?.package_sha256)}</p><pre>${esc(JSON.stringify(record.runtime?.capabilities || [], null, 2))}</pre></details>`
    : `<div class="install-progress" role="${failed ? "alert" : "status"}">${failed ? "" : `<span class="spinner" aria-hidden="true"></span>`}<p>${failed ? esc(request.message) : request.status === "installing" ? lx("正在安装并打开…", "Installing and opening…") : lx("正在获取并校验应用…", "Getting and checking the app…")}</p></div>`}
    <footer class="install-actions"><button class="secondary-button" data-dismiss-store-open="${esc(request.request_id)}" ${request.status === "installing" ? "disabled" : ""}>${failed ? lx("关闭", "Close") : lx("取消", "Cancel")}</button>
    ${ready ? `<button class="primary-button" data-confirm-store-open="${esc(request.request_id)}">${lx("安装并打开", "Install and open")}</button>` : ""}</footer>
  </section>`;
}

async function pollStoreOpenRequest() {
  if (!window.__TAURI__?.core?.invoke || !model.installWindow || model.storeOpenPolling) return;
  model.storeOpenPolling = true;
  try {
    const next = await call("get_store_open_request");
    if (JSON.stringify(next) !== JSON.stringify(model.storeOpenRequest)) {
      model.storeOpenRequest = next;
      render({ background: true });
    }
  } catch (_) { /* Older native clients have no link handler; normal UI remains usable. */ }
  finally { model.storeOpenPolling = false; }
}

function render({ background = false } = {}) {
  if (model.installWindow) {
    view.innerHTML = renderStoreOpenRequest();
    // Consent must never be the default keyboard action on a newly opened pane.
    document.querySelector("[data-dismiss-store-open]:not([disabled])")?.focus({ preventScroll: true });
    return;
  }
  // Polling must not replace an editor's DOM, focus or unsaved values. In
  // particular, do not persist BYOM credentials just to preserve a draft.
  if (background && hasUnsavedFormEdits()) return;
  document.querySelectorAll("[data-route]").forEach(button => button.classList.toggle("active", button.dataset.route === model.route || (model.route === "runtime" && button.dataset.route === "apps")));
  if (!model.data) return;
  const renderers = {
    home: () => model.conversation.length ? renderConversation() : renderSearchHome(),
    apps: renderApps,
    builds: renderBuilds,
    runtime: renderRuntime,
    settings: renderSettings,
  };
  view.innerHTML = (renderers[model.route] || renderSearchHome)();
  updateLocaleUI();
  bindComposerSizing();
  document.querySelectorAll('textarea[data-runtime-kind="text"]').forEach(control => {
    const resize = () => { control.style.height = "auto"; control.style.height = `${Math.min(280, Math.max(44, control.scrollHeight))}px`; };
    control.addEventListener("input", resize);
    resize();
  });
  bindRuntimeLayout();
  scheduleConsentExpiryRefresh();
}

function scheduleConsentExpiryRefresh() {
  if (model.consentExpiryTimer !== null && typeof window.clearTimeout === "function") {
    window.clearTimeout(model.consentExpiryTimer);
  }
  model.consentExpiryTimer = null;
  if (model.route !== "home") return;
  const expiries = model.conversation
    .map(item => developmentTaskIdentity(item.result)?.expiresAtUtc)
    .map(value => typeof value === "string" ? Date.parse(value) : Number.NaN)
    .filter(value => Number.isFinite(value) && value > Date.now());
  if (!expiries.length) return;
  const delay = Math.min(Math.max(1, Math.min(...expiries) - Date.now() + 25), 2_147_483_647);
  model.consentExpiryTimer = window.setTimeout(() => {
    model.consentExpiryTimer = null;
    if (model.route === "home") render();
  }, delay);
}

function navigate(route) {
  if (route === "apps") model.storeDetailOpen = false;
  model.route = ["home", "apps", "builds", "runtime", "settings"].includes(route) ? route : "home";
  render();
  main.focus({ preventScroll: true });
}

function bindComposerSizing() {
  document.querySelectorAll(".composer textarea").forEach(input => {
    if (input.dataset.composerBound) return;
    input.dataset.composerBound = "true";
    let isComposing = false;
    let lastCompositionEndTime = 0;
    input.addEventListener("compositionstart", () => {
      isComposing = true;
    }, { capture: true });
    input.addEventListener("compositionend", () => {
      isComposing = false;
      lastCompositionEndTime = Date.now();
    }, { capture: true });
    const resize = () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
    };
    input.addEventListener("input", resize);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) {
        if (event.isComposing || event.keyCode === 229 || event.key === "Process" || isComposing || isGlobalComposing) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        if ((Date.now() - lastCompositionEndTime < 800) || (Date.now() - lastGlobalCompositionEndTime < 800)) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        event.preventDefault();
        input.form.requestSubmit();
      }
    });
    resize();
  });
}

function bindRuntimeLayout() {
  model.runtimeLayoutObserver?.disconnect();
  model.runtimeLayoutObserver = null;
  const surface = document.querySelector(".guest-surface");
  if (!surface) return;
  const label = document.querySelector("[data-layout-label]");
  const apply = width => {
    const layout = width < 560 ? "compact" : width < 900 ? "regular" : "wide";
    surface.dataset.layout = layout;
    if (label) {
      label.textContent = ({
        compact: t("page_runtime_layout_compact"),
        regular: t("page_runtime_layout_regular"),
        wide: t("page_runtime_layout_wide"),
      })[layout];
    }
  };
  apply(surface.getBoundingClientRect().width);
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(() => apply(surface.getBoundingClientRect().width));
    observer.observe(surface);
    model.runtimeLayoutObserver = observer;
  }
}

async function refreshCollaborationStatus() {
  model.collaborationStatus = await call("get_collaboration_status");
  return model.collaborationStatus;
}

document.addEventListener("click", async event => {
  if (event.target.closest("[data-client-download]")) {
    event.preventDefault();
    await openClientDownload();
    return;
  }
  const downloadDialog = document.querySelector("#client-download-dialog");
  if (event.target.closest("[data-close-download]") || (event.target === downloadDialog && downloadDialog?.open)) {
    // Only clicks outside the dialog's bounds dismiss its backdrop. Empty
    // space inside the panel must not unexpectedly close the picker.
    const rect = downloadDialog?.getBoundingClientRect();
    if (event.target !== downloadDialog || (rect && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom))) downloadDialog?.close();
    return;
  }
  const storeFilter = event.target.closest("[data-store-filter]");
  if (storeFilter || event.target.closest("[data-reset-store]") || event.target.closest("[data-back-store]")) {
    model.storeDetailOpen = false;
    if (storeFilter) model.storeFilter = storeFilter.dataset.storeFilter;
    if (event.target.closest("[data-reset-store]")) { model.storeFilter = "all"; model.storeQuery = ""; }
    render();
    document.querySelector(storeFilter ? `[data-store-filter="${model.storeFilter}"]` : "[data-store-search]")?.focus({ preventScroll: true });
    return;
  }
  const desktopLink = event.target.closest("[data-open-desktop-app]");
  if (desktopLink) {
    // Keep navigation in the original user gesture. Do not infer installation
    // from a timer/blur event, or remove the anchor before its default action.
    model.desktopHandoffAppId = desktopLink.dataset.openDesktopApp;
    model.selectedAppId = desktopLink.dataset.openDesktopApp;
    model.storeDetailOpen = true;
    window.setTimeout(() => render(), 200);
    return;
  }
  const confirmStore = event.target.closest("[data-confirm-store-open]");
  const dismissStore = event.target.closest("[data-dismiss-store-open]");
  if (confirmStore || dismissStore) {
    event.preventDefault();
    if (model.busy) return;
    model.busy = true;
    try {
      const requestId = confirmStore?.dataset.confirmStoreOpen || dismissStore.dataset.dismissStoreOpen;
      if (confirmStore) {
        model.storeOpenRequest.status = "installing";
        render();
        await call("confirm_store_open_request", { requestId });
      } else await call("dismiss_store_open_request", { requestId });
      model.storeOpenRequest = null;
    } catch (error) { showToast(String(error), true); }
    finally { model.busy = false; await pollStoreOpenRequest(); render(); }
    return;
  }
  const route = event.target.closest("[data-route]");
  const suggestion = event.target.closest("[data-suggestion]");
  const app = event.target.closest("[data-app-id]");
  const launch = event.target.closest("[data-launch]");
  const installApp = event.target.closest("[data-install-app]");
  const appAction = event.target.closest("[data-app-action]");
  const closeRuntime = event.target.closest("[data-close-runtime]");
  const developmentSubmit = event.target.closest("[data-submit-development]");
  const newChat = event.target.closest("[data-new-chat]");
  const openJobChat = event.target.closest("[data-open-job-chat]");
  const editJob = event.target.closest("[data-edit-job]");
  const jobApp = event.target.closest("[data-job-app]");
  const jobRun = event.target.closest("[data-job-run]");
  const runtimeAction = event.target.closest("[data-runtime-action]");
  const networkNodeAction = event.target.closest("[data-network-node-action]");
  const collaborationCreate = event.target.closest("[data-collaboration-create]");
  const collaborationJoin = event.target.closest("[data-collaboration-join]");
  const collaborationLeave = event.target.closest("[data-collaboration-leave]");
  if ((collaborationCreate || collaborationJoin || collaborationLeave) && !model.collaborationBusy) {
    const appId = model.runningApp?.descriptor?.id;
    if (!appId) return;
    const confirmed = document.querySelector("[data-collaboration-confirm]")?.checked === true;
    if (!collaborationLeave && !confirmed) {
      showToast(t("collaboration_confirm_required"), true);
      return;
    }
    const channelId = String(document.querySelector("[data-collaboration-channel]")?.value || "").trim();
    model.collaborationBusy = true;
    model.collaborationError = null;
    render();
    try {
      if (collaborationCreate) {
        await call("create_collaboration_session", { payload: { appId, confirmed: true, expiresInMs: 60 * 60 * 1000 } });
        showToast(t("collaboration_created"));
      } else if (collaborationJoin) {
        await call("join_collaboration_session", { payload: { appId, channelId, confirmed: true, expiresInMs: 60 * 60 * 1000 } });
        showToast(t("collaboration_joined"));
      } else {
        await call("leave_collaboration_session", { payload: { sessionId: collaborationLeave.dataset.collaborationLeave } });
        showToast(t("collaboration_left"));
      }
      await refreshCollaborationStatus();
    } catch (failure) {
      model.collaborationError = String(failure);
      showToast(String(failure), true);
    } finally {
      model.collaborationBusy = false;
      render();
    }
    return;
  }
  if (runtimeAction) {
    await dispatchRuntimeAction(runtimeAction.dataset.runtimeAction);
    return;
  }
  if (networkNodeAction && !model.busy) {
    const action = networkNodeAction.dataset.networkNodeAction;
    model.busy = true;
    networkNodeAction.disabled = true;
    networkNodeAction.innerHTML = `<span class="button-spinner"></span>${action === "stop" ? t("settings_network_stopping") : t("settings_network_starting")}`;
    try {
      model.networkStatus = await call(action === "stop" ? "stop_network_node" : "start_network_node");
      showToast(action === "stop" ? t("settings_network_stopped") : t("settings_network_started"));
    } catch (failure) {
      showToast(String(failure), true);
      model.networkStatus = await call("get_network_status").catch(() => model.networkStatus);
    } finally {
      model.busy = false;
      render();
    }
    return;
  }
  if (route) navigate(route.dataset.route);
  if (suggestion) {
    const input = document.querySelector(".composer textarea");
    input.value = suggestion.dataset.suggestion;
    input.dispatchEvent(new Event("input"));
    input.focus();
  }
  if (app) {
    model.selectedAppId = app.dataset.appId;
    model.storeDetailOpen = true;
    render();
    main.focus({ preventScroll: true });
  }
  if (newChat) {
    model.conversation = [];
    model.busy = false;
    model.activeNeedId = null;
    model.composerDraft = "";
    model.retryTaskId = null;
    model.retryNeedId = null;
    render();
    document.querySelector(".composer textarea")?.focus();
  }
  if (openJobChat) {
    const job = (model.data.jobs || []).find(item => (item.task_id || item.job_id) === openJobChat.dataset.openJobChat);
    if (job) {
      model.conversation = conversationForJob(job);
      await verifyConversationDevelopmentResults(model.conversation);
      model.activeNeedId = null;
      model.route = "home";
      render();
    }
  }
  if (editJob) {
    const records = (model.data.needs || []).filter(item => item.need_id === editJob.dataset.editNeed);
    const need = records.at(-1);
    const job = (model.data.jobs || []).find(item => (item.task_id || item.job_id) === editJob.dataset.editJob);
    model.retryTaskId = editJob.dataset.editJob || null;
    model.retryNeedId = editJob.dataset.editNeed || null;
    model.composerDraft = need?.description || "";
    model.conversation = job ? conversationForJob(job) : [];
    await verifyConversationDevelopmentResults(model.conversation);
    model.route = "home";
    render();
    document.querySelector(".composer textarea")?.focus();
  }
  if (jobApp) {
    model.storeDetailOpen = true;
    model.selectedAppId = jobApp.dataset.jobApp;
    model.route = "apps";
    render();
  }
  if (jobRun && !model.busy) {
    const currentApp = (model.data?.apps || []).find(item => item.app_id === jobRun.dataset.jobRun);
    if (!currentApp
      || currentApp.launch_eligible !== true
      || currentApp.package_digest_sha256 !== jobRun.dataset.jobPackageDigest) {
      showToast(lx("这个任务对应的应用版本已不是当前可运行版本，请到应用详情确认后再启动。", "This job no longer matches the active runnable package. Review the app details before launching."), true);
      return;
    }
    model.busy = true;
    try {
      if (window.__TAURI__?.core?.invoke && !model.runtimeWindowAppId) {
        const opened = await call("open_app_window", { appId: jobRun.dataset.jobRun });
        showToast(opened?.created ? lx("应用已在独立窗口中打开。", "App opened in its own window.") : lx("已切换到应用窗口。", "Switched to the app window."));
      } else {
        model.runningApp = await call("launch_app", { appId: jobRun.dataset.jobRun });
        model.runtimeError = null;
        model.runtimeDraft = {};
        model.route = "runtime";
        model.runtimeRefreshActive = runtimeInteractive();
        scheduleRuntimeSurfaceRefresh();
      }
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
  }
  if (closeRuntime && !model.busy) {
    if (model.runtimeWindowAppId) {
      model.busy = true;
      stopRuntimeSurfaceRefresh();
      try {
        await call("close_app_window");
      } catch (failure) {
        model.busy = false;
        showToast(String(failure), true);
        render();
      }
      return;
    }
    const running = model.runningApp;
    const daemonSurface = running?.daemon_surface?.outcome?.value;
    model.busy = true;
    try {
      if (running?.launcher_context?.installed && daemonSurface?.session && daemonSurface?.surface) {
        await call("control_app_lifecycle", { payload: {
          app_id: running.descriptor.id,
          action: "surface-close",
          entrypoint: null,
          disposition: null,
          session: daemonSurface.session,
          surface: daemonSurface.surface,
          trigger_id: null,
          payload: null,
        } });
        model.data = await call("get_state");
      }
      if (running?.launcher_context?.foreground_interactive) await call("close_app_window");
      stopRuntimeSurfaceRefresh();
      model.runningApp = null;
      model.runtimeError = null;
      model.runtimeDraft = {};
      model.route = "apps";
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
  }
  if (installApp && !model.busy) {
    model.busy = true;
    installApp.disabled = true;
    installApp.innerHTML = `<span class="button-spinner"></span>${model.locale === "en-US" ? "Installing" : "正在安装"}`;
    try {
      const result = await call("submit_install_intent", { payload: { app_id: installApp.dataset.installApp, package_digest_sha256: installApp.dataset.packageDigest } });
      model.data = await call("get_state");
      if (result?.intent?.installation_performed === true) {
        showToast(model.locale === "en-US" ? "Installed privately; enable it when ready." : "已私有安装；确认后可单独启用。");
      } else if (result?.intent?.web_package_cached === true) {
        showToast(result.intent.web_runtime_available
          ? (model.locale === "en-US" ? "Verified package cached. The verified web runtime is ready." : "公开包已缓存并验真；可使用已验证的网页运行版本。")
          : (model.locale === "en-US" ? "Verified package cached. Use VibApp Client to run it." : "公开包已缓存并验真；请用 VibApp Client 运行。"));
      } else {
        showToast(model.locale === "en-US" ? "This app is not installed yet; a compatible web package or the desktop client is required." : "这个应用尚未安装；需要可兼容的网页包或桌面客户端。", true);
      }
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
  }
  if (appAction && !model.busy) {
    model.busy = true;
    appAction.disabled = true;
    const action = appAction.dataset.appAction;
    try {
      if (action === "update" && !window.confirm(model.locale === "en-US" ? "Update this app now? VibApp will migrate a copy of its state and restore the current version if activation fails." : "现在更新这个应用？VibApp 会迁移一份状态副本；若启用失败，会恢复当前版本。")) {
        return;
      }
      await call("control_app_lifecycle", { payload: {
        app_id: appAction.dataset.actionApp,
        action,
        entrypoint: appAction.dataset.entrypoint || null,
        disposition: appAction.dataset.disposition || null,
        session: null,
        surface: null,
        trigger_id: null,
        payload: null,
        package_digest_sha256: appAction.dataset.packageDigest || null,
      } });
      model.data = await call("get_state");
      if (action === "uninstall") model.selectedAppId = null;
      showToast(action === "update" ? (model.locale === "en-US" ? "Update activated and is under observation." : "更新已启用，正在观察运行状态。") : (model.locale === "en-US" ? "App lifecycle updated." : "应用生命周期状态已更新。"));
    } catch (failure) {
      if (action === "update") {
        try { model.data = await call("get_state"); } catch (_) { /* preserve the original update error */ }
      }
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
  }
  if (developmentSubmit && !model.busy) {
    const prepared = findPreparedDevelopmentTask(model.conversation, {
      immutableTaskDigest: developmentSubmit.dataset.submitDevelopment,
      attemptId: developmentSubmit.dataset.submitAttempt,
      consentId: developmentSubmit.dataset.submitConsent,
      expiresAtUtc: developmentSubmit.dataset.submitExpires,
      registryRequestId: developmentSubmit.dataset.submitRegistryRequest,
      registryEvidenceSha256: developmentSubmit.dataset.submitRegistryEvidence,
    });
    if (!prepared) {
      showToast(t("toast_missing_schema"), true);
      return;
    }
    const { message, task } = prepared;
    const explicitConfirmation = developmentSubmit.parentElement?.querySelector("[data-codeagent-submit-confirmation]")?.checked === true;
    if (!explicitConfirmation) {
      showToast(t("toast_state_required"), true);
      return;
    }
    const externalCostAcknowledged = developmentSubmit.parentElement?.querySelector("[data-external-cost-confirmation]")?.checked === true;
    if (!externalCostAcknowledged) {
      showToast(t("toast_external_cost_required"), true);
      return;
    }
    model.busy = true;
    developmentSubmit.disabled = true;
    developmentSubmit.innerHTML = `<span class="button-spinner"></span>${t("queueing_development")}`;
    try {
      const queued = await call("submit_development_task", {
        payload: { task, registry: message.result.registry, explicit_user_submit: true, acknowledge_external_cost: true, retry_task_id: model.retryTaskId },
      });
      message.result.cloud_development.local_queue_receipt = queued.receipt;
      message.result.cloud_development.local_codeagent_adapter = queued.codeagent_adapter;
      model.data = await call("get_state");
      if (queued.codeagent_adapter?.started) {
        showToast(t("development_queued"));
      } else {
        showToast(`${t("development_queue_failed_prefix")}${queued.codeagent_adapter?.error || t("toast_unknown_error")}`, true);
      }
      model.retryTaskId = null;
      model.retryNeedId = null;
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
  }
  if (launch && !model.busy) {
    model.busy = true;
    launch.disabled = true;
    launch.innerHTML = `<span class="button-spinner"></span>${t("start_runtime")}`;
    try {
      if (window.__TAURI__?.core?.invoke && !model.runtimeWindowAppId) {
        const opened = await call("open_app_window", { appId: launch.dataset.launch });
        showToast(opened?.created ? lx("应用已在独立窗口中打开。", "App opened in its own window.") : lx("已切换到应用窗口。", "Switched to the app window."));
      } else {
        model.runningApp = await call("launch_app", { appId: launch.dataset.launch });
        model.runtimeError = null;
        model.runtimeDraft = {};
        model.route = "runtime";
        model.runtimeRefreshActive = runtimeInteractive();
        scheduleRuntimeSurfaceRefresh();
      }
      model.busy = false;
      render();
    } catch (failure) {
      showToast(String(failure), true);
      model.busy = false;
      render();
    }
  }
});

document.addEventListener("keydown", event => {
  if (model.installWindow && event.key === "Escape" && !event.isComposing) {
    event.preventDefault();
    document.querySelector("[data-dismiss-store-open]:not([disabled])")?.click();
    return;
  }
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229 || isGlobalComposing || (Date.now() - lastGlobalCompositionEndTime < 500) || !event.target.closest("[data-runtime-field]")) return;
  const candidates = [...document.querySelectorAll('.runtime-button.primary[data-runtime-action]:not([disabled])')];
  if (candidates.length !== 1) return;
  event.preventDefault();
  candidates[0].click();
});

document.addEventListener("input", event => {
  if (!event.target.matches?.("[data-store-search]")) return;
  model.storeQuery = event.target.value;
  const results = document.querySelector("#store-results");
  if (results) results.innerHTML = renderStoreResults();
});

document.addEventListener("change", event => {
  const control = event.target;
  if (!control.matches?.("[data-download-os], [data-download-arch]")) return;
  const current = model.downloadDevice || clientDevice();
  model.downloadDevice = control.matches("[data-download-os]")
    ? { os: control.value, arch: "unknown" }
    : { ...current, arch: control.value };
  const selector = control.matches("[data-download-os]") ? "[data-download-os]" : "[data-download-arch]";
  const dialog = document.querySelector("#client-download-dialog");
  if (dialog?.open) {
    dialog.innerHTML = renderClientDownload();
    dialog.querySelector(selector)?.focus();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!model.runtimeWindowAppId && !model.runningApp?.launcher_context?.foreground_interactive) return;
  if (document.visibilityState === "hidden") {
    stopRuntimeSurfaceRefresh(false);
    return;
  }
  model.runtimeRefreshActive = runtimeInteractive();
  scheduleRuntimeSurfaceRefresh();
});

document.addEventListener("change", event => {
  const kindSelect = event.target.closest("[data-app-kind]");
  if (kindSelect) {
    const form = kindSelect.closest(".needspec-form");
    const preset = worldPresets[kindSelect.value] || worldPresets.ui;
    const worldLabel = form.querySelector("[data-world-label]");
    if (worldLabel) worldLabel.textContent = `${preset.world} · ${lx("安全沙箱环境", "sandboxed world")}`;
    form.querySelectorAll("[data-capability]").forEach(input => { input.checked = preset.capabilities.includes(input.value); });
    form.querySelectorAll("[data-allowed]").forEach(input => { input.checked = preset.permissions.includes(input.value); });
    form.querySelectorAll("[data-forbidden]").forEach(input => { input.checked = !preset.permissions.includes(input.value) && input.value === "http"; });
    const netMode = form.querySelector("[data-network-mode]");
    if (netMode) netMode.value = preset.network;
    const chipsContainer = form.querySelector("[data-permission-chips]");
    if (chipsContainer) {
      chipsContainer.innerHTML = renderPermissionChips(preset.permissions);
    }
    return;
  }

  const localeSelect = event.target.closest("[data-setting-locale]");
  if (localeSelect) {
    setLocale(localeSelect.value || "auto");
  }
  const codeAgentSelect = event.target.closest("[data-codeagent-provider]");
  if (codeAgentSelect && model.codeAgentSettings) {
    const currentModel = String(codeAgentSelect.form?.elements?.codeagent_model?.value || "").trim();
    model.codeAgentSettings.modelByProvider = model.codeAgentSettings.modelByProvider || {};
    const previous = model.codeAgentSettings.selectedProvider;
    if (currentModel) model.codeAgentSettings.modelByProvider[previous] = currentModel;
    model.codeAgentSettings.selectedProvider = codeAgentSelect.value;
    render();
  }
});

document.addEventListener("submit", async event => {
  if (hostedServiceUnavailable() && ["hero-composer", "chat-composer"].includes(event.target.id)) {
    event.preventDefault();
    return;
  }
  if (isGlobalComposing || (Date.now() - lastGlobalCompositionEndTime < 800)) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (event.target.classList.contains("network-settings-form")) {
    event.preventDefault();
    if (model.busy) return;
    const data = new FormData(event.target);
    const payload = {
      p2pEnabled: data.get("p2p_enabled") === "on",
      seedVerifiedApps: data.get("seed_verified_apps") === "on",
      allowUserFileSeeding: data.get("allow_user_file_seeding") === "on",
      uploadLimitKibPerSecond: Number(data.get("upload_limit")),
      downloadLimitKibPerSecond: Number(data.get("download_limit")),
      cacheLimitMib: Number(data.get("cache_limit")),
      maxActiveTransfers: Number(data.get("max_transfers")),
      rtcEnabled: data.get("rtc_enabled") === "on",
      maxActiveChannels: Number(data.get("max_channels")),
      turnEnabled: data.get("turn_enabled") === "on",
      turnUrls: String(data.get("turn_urls") || "").split(/\r?\n/).map(value => value.trim()).filter(Boolean),
      turnUsername: String(data.get("turn_username") || "").trim(),
      turnCredential: String(data.get("turn_credential") || "").trim() || null,
      clearTurnCredential: data.get("clear_turn_credential") === "on",
    };
    const button = event.target.querySelector("[data-save-network-settings]");
    model.busy = true;
    if (button) {
      button.disabled = true;
      button.innerHTML = `<span class="button-spinner"></span>${t("settings_network_saving")}`;
    }
    try {
      model.networkSettings = await call("save_network_settings", { payload });
      model.networkStatus = await call(payload.p2pEnabled ? "start_network_node" : "stop_network_node");
      showToast(t("settings_network_saved"));
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
    return;
  }
  if (event.target.classList.contains("codeagent-settings-form")) {
    event.preventDefault();
    if (model.busy || !model.codeAgentSettings) return;
    const data = new FormData(event.target);
    const selectedProvider = String(data.get("selected_provider") || "codex");
    const selectedModel = String(data.get("codeagent_model") || "").trim();
    if (!selectedModel) {
      showToast(t("settings_codeagent_model_hint"), true);
      return;
    }
    const modelByProvider = { ...(model.codeAgentSettings.modelByProvider || {}) };
    modelByProvider[selectedProvider] = selectedModel;
    const button = event.target.querySelector("[data-save-codeagent-settings]");
    model.busy = true;
    if (button) {
      button.disabled = true;
      button.innerHTML = `<span class="button-spinner"></span>${t("settings_codeagent_saving")}`;
    }
    try {
      model.codeAgentSettings = await call("save_codeagent_settings", {
        payload: { selectedProvider, modelByProvider },
      });
      model.data = await call("get_state");
      showToast(t("settings_codeagent_saved"));
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
    return;
  }
  if (event.target.classList.contains("model-settings-form")) {
    event.preventDefault();
    if (model.busy) return;
    const data = new FormData(event.target);
    const payload = {
      generation: {
        enabled: data.get("generation_enabled") === "on",
        baseUrl: String(data.get("generation_base_url") || "").trim(),
        model: String(data.get("generation_model") || "").trim(),
        protocol: String(data.get("generation_protocol") || "chat-completions"),
        timeoutSeconds: Number(data.get("generation_timeout")),
        maxOutputTokens: Number(data.get("generation_max_tokens")),
        temperature: Number(data.get("generation_temperature")),
        apiKey: String(data.get("generation_api_key") || "").trim() || null,
        clearApiKey: data.get("generation_clear_api_key") === "on",
      },
      embedding: {
        enabled: data.get("embedding_enabled") === "on",
        baseUrl: String(data.get("embedding_base_url") || "").trim(),
        model: String(data.get("embedding_model") || "").trim(),
        dimensions: Number(data.get("embedding_dimensions")),
        timeoutSeconds: Number(data.get("embedding_timeout")),
        apiKey: String(data.get("embedding_api_key") || "").trim() || null,
        clearApiKey: data.get("embedding_clear_api_key") === "on",
      },
    };
    const button = event.target.querySelector("[data-save-model-settings]");
    model.busy = true;
    if (button) {
      button.disabled = true;
      button.innerHTML = `<span class="button-spinner"></span>${t("settings_saving")}`;
    }
    try {
      model.modelSettings = await call("save_model_settings", { payload });
      showToast(t("settings_saved"));
    } catch (failure) {
      showToast(String(failure), true);
    } finally {
      model.busy = false;
      render();
    }
    return;
  }
  if (event.target.classList.contains("needspec-form")) {
    event.preventDefault();
    if (model.busy) return;
    const data = new FormData(event.target);
    const payload = {
      need_id: event.target.dataset.needId,
      app_kind: String(data.get("app_kind") || ""),
      capabilities: [...new Set(data.getAll("capabilities").map(String))],
      allowed_permissions: [...new Set(data.getAll("allowed_permissions").map(String))],
      forbidden_permissions: [...new Set(data.getAll("forbidden_permissions").map(String))],
      permission_ceiling_confirmed: data.get("permission_ceiling_confirmed") === "on",
      negative_constraints: String(data.get("negative_constraints") || "").trim(),
      negative_constraints_confirmed: data.get("negative_constraints_confirmed") === "on",
      network_mode: String(data.get("network_mode") || ""),
      package_id: String(data.get("package_id") || "").trim(),
      package_name: String(data.get("package_name") || "").trim(),
      package_version: String(data.get("package_version") || "").trim(),
      acceptance_example: String(data.get("acceptance_example") || "").trim(),
      proceed_after_recommendation: ["on", "granted"].includes(String(data.get("proceed_after_recommendation") || "")),
      registry_embedding_consent: data.get("registry_embedding_consent") === "on",
      remote_processing_consent: data.get("remote_processing_consent") === "on",
      public_publication_consent: data.get("public_publication_consent") === "on",
    };
    model.conversation.push({ role: "user", needId: payload.need_id, text: lx(`确认需求并开始构建：${payload.package_name || payload.package_id}`, `Confirm requirements and build app: ${payload.package_name || payload.package_id}`) });
    model.busy = true;
    render();
    try {
      const result = await call("complete_need", { payload });
      await verifyDevelopmentResultIntegrity(result);
      model.conversation.push({ role: "assistant", result });
      model.activeNeedId = result.need_spec?.state === "complete" ? null : result.need?.need_id;
      model.data = await call("get_state");

      const preparedIdentity = developmentTaskIdentity(result);
      if (preparedIdentity && result.need_spec?.state === "complete" && result.registry?.route !== "recommendation") {
        const prepared = findPreparedDevelopmentTask(model.conversation, preparedIdentity);
        if (prepared) {
          try {
            const queued = await call("submit_development_task", {
              payload: {
                task: prepared.task,
                registry: result.registry,
                explicit_user_submit: true,
                acknowledge_external_cost: true,
                retry_task_id: model.retryTaskId,
              },
            });
            result.cloud_development.local_queue_receipt = queued.receipt;
            result.cloud_development.local_codeagent_adapter = queued.codeagent_adapter;
            model.data = await call("get_state");
            if (queued.codeagent_adapter?.started) {
              showToast(t("development_queued"));
            } else {
              showToast(`${t("development_queue_failed_prefix")}${queued.codeagent_adapter?.error || t("toast_unknown_error")}`, true);
            }
            model.retryTaskId = null;
            model.retryNeedId = null;
          } catch (queueErr) {
            console.warn("Auto-submit development task error:", queueErr);
          }
        }
      }
    } catch (failure) {
      model.conversation.push({ role: "assistant", error: true, text: String(failure) });
    } finally {
      model.busy = false;
      render();
    }
    return;
  }
  if (!event.target.classList.contains("composer")) return;
  event.preventDefault();
  if (model.busy) return;
  if (isGlobalComposing || (Date.now() - lastGlobalCompositionEndTime < 800)) return;
  const data = new FormData(event.target);
  const description = String(data.get("description") || "").trim();
  if (description.length < 10) {
    event.target.querySelector(".composer-error").textContent = t("search_placeholder_error");
    return;
  }
  const title = description.replace(/[。！!？?，,].*$/, "").slice(0, 40) || t("composer_name_default");
  const embeddingConsent = data.get("embedding_consent") === "granted";
  model.conversation.push({ role: "user", text: description });
  model.route = "home";
  model.busy = true;
  render();
  try {
    const result = await call("submit_need", { payload: {
      title,
      description,
      embedding_consent: embeddingConsent,
      retry_task_id: model.retryTaskId,
      need_id: model.retryNeedId,
    } });
    model.composerDraft = "";
    model.conversation.push({ role: "assistant", result });
    model.activeNeedId = result.need?.need_id || null;
    model.data = await call("get_state");
  } catch (failure) {
    model.conversation.push({ role: "assistant", error: true, text: String(failure) });
  } finally {
    model.busy = false;
    render();
  }
});

function runtimeInitialContentHeight() {
  const guest = document.querySelector(".guest-surface");
  if (!guest || guest.classList.contains("single-display") || !guest.children.length) return null;
  const style = window.getComputedStyle(guest);
  const first = guest.firstElementChild.getBoundingClientRect();
  const last = guest.lastElementChild.getBoundingClientRect();
  const strip = document.querySelector(".runtime-trust-strip");
  const height = Math.ceil(last.bottom - first.top + (parseFloat(style.paddingTop) || 0)
    + (parseFloat(style.paddingBottom) || 0) + (strip?.getBoundingClientRect().height || 0));
  return Number.isFinite(height) && height > 0 ? Math.min(1200, Math.max(220, height)) : null;
}

async function boot() {
  await window.VibAppWebBridge?.ready;
  model.localePreference = getLocalePreference();
  model.locale = resolveLocale();
  updateLocaleUI();
  if (model.installWindow) {
    document.body?.classList.add("install-host-window");
    await pollStoreOpenRequest();
    return;
  }
  if (model.runtimeWindowAppId) document.body?.classList.add("runtime-host-window");
  try {
    [model.data, model.modelSettings, model.codeAgentSettings, model.networkSettings, model.networkStatus, model.collaborationStatus] = await Promise.all([
      call("get_state"),
      call("get_model_settings").catch(() => null),
      call("get_codeagent_settings").catch(() => null),
      call("get_network_settings").catch(() => null),
      call("get_network_status").catch(() => ({ state: "unavailable", foregroundOnly: Boolean(window.VibAppWebBridge?.invoke), activeTransfers: 0, torrents: 0, lastError: null })),
      call("get_collaboration_status").catch(() => ({ state: "unavailable", sessionCount: 0, queuedEvents: 0, sessions: [] })),
    ]);
    if (model.runtimeWindowAppId) {
      const selected = (model.data?.apps || []).find(app => app.app_id === model.runtimeWindowAppId);
      if (!selected?.launch_eligible) throw new Error(lx("这个应用当前不可启动。", "This app is not currently launchable."));
      model.selectedAppId = model.runtimeWindowAppId;
      model.runningApp = await call("launch_app", { appId: model.runtimeWindowAppId });
      const binding = model.runningApp?.runtime_binding;
      const registeredSurface = model.runningApp?.launcher_context?.installed && binding?.session && binding?.surface;
      if (registeredSurface) {
        await call("register_app_window_surface", { payload: {
          appId: model.runtimeWindowAppId, session: binding.session, surface: binding.surface,
        } });
      }
      model.route = "runtime";
      document.title = `${selected.display_name || model.runningApp?.descriptor?.display_name || "VibApp"} — VibApp`;
      model.runtimeRefreshActive = runtimeInteractive();
      render();
      // Running the app must not wait for a cosmetic animation frame. WebKit
      // can suspend requestAnimationFrame while a newly opened window is occluded.
      scheduleRuntimeSurfaceRefresh();
      if (registeredSurface) {
        // Fit once after layout, never on every clock tick or user action.
        // This is host-measured content, not guest-controlled window metadata.
        window.requestAnimationFrame(() => {
          const contentHeight = runtimeInitialContentHeight();
          if (contentHeight == null) return;
          void call("register_app_window_surface", { payload: {
            appId: model.runtimeWindowAppId,
            session: binding.session,
            surface: binding.surface,
            contentHeight,
          } }).catch(() => { /* Cosmetic sizing must not invalidate a working surface. */ });
        });
      }
      return;
    }
    const sharedAppId = new URLSearchParams(window.location.search).get("app");
    if (sharedAppId && (model.data?.apps || []).some(app => app.app_id === sharedAppId)) {
      model.selectedAppId = sharedAppId;
      model.storeDetailOpen = true;
      model.route = "apps";
    }
    render();
  } catch (failure) {
    view.innerHTML = `<div class="fatal-state"><strong>${t("boot_failed")}</strong><p>${esc(failure)}</p></div>`;
  }
}

window.VibAppUiTest = {
  model,
  i18n: I18N,
  renderMessage,
  renderNeedSpecForm,
  renderCompletedNeed,
  renderRecommendation,
  renderApps,
  desktopAppUrl, webAppOpenMode, renderStoreOpenRequest,
  clientDevice, renderClientDownload, openClientDownload, CLIENT_RELEASE,
  renderStore, renderStoreResults, filteredStoreApps,
  renderBuilds,
  renderJobConversation,
  renderCodeagentDiagnostics,
  renderRuntime,
  renderSettings,
  renderRuntimeTree,
  hashAppIdentity,
  appIdentityModel,
  appIdentityIcon,
  publicationBadge,
  runtimeTrustedIdentity,
  renderRuntimeTrustStrip,
  runtimeWireValue,
  acceptRuntimeActionResult,
  runtimeEventId,
  runtimeSurfaceIsDisplayOnly,
  runtimeSurfaceIsSingleDisplay,
  runtimeInitialContentHeight,
  refreshRuntimeSurface,
  hasUnsavedFormEdits,
  render,
  dispatchRuntimeAction,
  conversationForJob,
  canonicalJson,
  verifyDevelopmentResultIntegrity,
  verifyConversationDevelopmentResults,
  developmentTaskIdentity,
  consentBindingCurrentlyValid,
  findPreparedDevelopmentTask,
  setLocale, normalizeLocale, getDefaultLocale, resolveLocale, getLocalePreference, I18N,
  captureViewDrafts, restoreViewDrafts, renderSearchHome, hostedServiceUnavailable,
  setTestLocale(locale) { model.locale = locale; model.localePreference = locale; },
};

if (!window.__VIBAPP_UI_TEST__) {
  model.locale = resolveLocale();
  updateLocaleUI();
  window.addEventListener("languagechange", () => { if (model.localePreference === "auto") setLocale("auto"); });
  window.addEventListener("scroll", () => document.querySelector(".app-header")?.classList.toggle("scrolled", window.scrollY > 8), { passive: true });
  boot();
  window.setInterval(pollStoreOpenRequest, 1000);
  window.setInterval(async () => {
    if (model.route !== "settings" || model.busy || (!window.__TAURI__?.core?.invoke && !window.VibAppWebBridge?.invoke)) return;
    try {
      const next = await call("get_network_status");
      if (JSON.stringify(next) !== JSON.stringify(model.networkStatus)) {
        model.networkStatus = next;
        render({ background: true });
      }
    } catch (_) {
      // Status is also refreshed by explicit start/stop and settings actions.
    }
  }, 3000);
  window.setInterval(async () => {
    if (model.route !== "runtime" || !model.runningApp || model.collaborationBusy || (!window.__TAURI__?.core?.invoke && !window.VibAppWebBridge?.invoke)) return;
    try {
      const next = await call("get_collaboration_status");
      if (JSON.stringify(next) !== JSON.stringify(model.collaborationStatus)) {
        model.collaborationStatus = next;
        render({ background: true });
      }
    } catch (_) {
      // Explicit create/join/leave actions surface actionable failures.
    }
  }, 2000);
  window.setInterval(async () => {
    const active = (model.data?.jobs || []).some(job => ["codeagent-starting", "codeagent-running"].includes(job.status));
    if (!active || model.busy || (!window.__TAURI__?.core?.invoke && !window.VibAppWebBridge?.invoke)) return;
    try {
      model.data = await call("get_state");
      render({ background: true });
    } catch (_) {
      // The next user action or polling tick can recover; avoid disruptive toasts.
    }
  }, 2000);
}
