# -*- coding: utf-8 -*-
"""内置产品适配：各家网页的元素选择器和开关文字。

每类选择器是一个候选列表，按顺序尝试，第一个能在页面上找到元素的生效：
  input     输入框
  send      发送按钮（回车发不出去时点它）
  stop      停止生成按钮（出现时视为还在生成）
  response  一条回答的正文容器，取页面上最后一个作为最新回答
  exclude   回答容器里要去掉的部分：思考过程、引用卡片、操作栏、推荐追问
  scroll    对话滚动区域（长截图沿它滚动拼接）
  sidebar_close / sidebar  收起侧边栏的按钮 / 侧边栏本身（历史对话是隐私，采集时收起，收不起来就模糊）
文字类字段（mode_reasoning / mode_search / incognito_*）作为该产品的默认值，用户在“产品设置”里改过的优先。

options：采集前可以选的模型/模式。每项：
  id / label   标识和显示名
  kind         menu：先点 trigger 打开菜单（有 submenu 再点一层），再点文字匹配的 items
               radio：选项直接摆在页面上，没有 trigger，直接点文字匹配的 items
               toggle：开关按钮；selector 找按钮（可配 text 过滤），或只用 text 按文字找
  choices      已知的选项名（界面里也可以手填，产品改名后不用等更新）
  on_class / on_attr  toggle 的“已开启”判断：类名包含 / 属性等于（不填则看 aria-pressed 等通用属性）

网页随时会改版：选择器失效时，引擎会自动退回到通用识别，并在日志里提示。
到“产品 → 试跑”可以一键检查每一项是否还有效。
"""

SEL_KINDS = ("input", "send", "stop", "response", "exclude", "scroll", "sidebar_close", "sidebar")

# 没有内置适配的产品（含用户自己添加的）：按页面上的按钮文字找深度思考/联网开关
GENERIC_OPTIONS = [
    {"id": "think", "label": "深度思考", "kind": "toggle", "text_field": "mode_reasoning"},
    {"id": "search", "label": "联网搜索", "kind": "toggle", "text_field": "mode_search"},
]

# 来源：2026 年 9–10 月仍在更新的开源导出脚本、多站同问插件和 Playwright 采集项目，见各条 note。
ADAPTERS = {
    "chatgpt": {
        "updated": "2026-10",
        "options": [
            {"id": "mode", "label": "模式", "kind": "radio", "choices": ["聊天", "工作"],
             "items": ["button[aria-pressed]"], "note": "2026-10-30 实测登录后：顶部“聊天/工作”，两种模式下可选的模型和思考强度不同"},
            {"id": "model", "label": "模型", "kind": "menu",
             "trigger": ['button[data-composer-navigation-target="reasoning"]', "button[data-selected-reasoning-effort]"],
             "submenu": ["[data-model-picker-view-toggle]"], "submenu_optional": True,
             "items": ['[role="menu"] [role="menuitemradio"]', '[role="menuitemradio"]'],
             "choices_by": {"on": "mode", "map": {"聊天": ["GPT-6", "GPT-5.6 Sol"],
                                                 "工作": ["默认", "GPT-6.1 Sol", "GPT-6.1 Terra", "GPT-6 Astra", "GPT-6 Sol", "GPT-6 Luna",
                                                        "GPT-5.6 Sol", "GPT-5.6 Terra", "GPT-5.6 Luna"]}}},
            {"id": "effort", "label": "思考强度", "kind": "menu",
             "trigger": ['button[data-composer-navigation-target="reasoning"]', "button[data-selected-reasoning-effort]"],
             "slider": ['[data-reasoning-slider] [role="slider"]', '[role="menu"] [role="slider"]'],
             "choices_by": {"on": "mode", "map": {"聊天": ["Instant", "Medium", "High", "Extra High", "Pro"],
                                                 "工作": ["无", "极低", "轻度", "中", "高", "极高", "Max", "Ultra", "持续"]}},
             "note": "菜单里的滑杆；工作模式下不同模型可选的档位不同"},
        ],
        "note": "2026-10 改版后新旧几套页面并存，最新的在前（2026-10-29 实测未登录页面）。"
                "来源：实测、gemini-voyager、ai-chat-to-markdown、web-session-automation",
        "input": ['form[data-chatgpt-composer] [contenteditable="true"][role="textbox"]', "#prompt-textarea",
                  '[data-composer-markdown][contenteditable="true"]'],
        "send": ["button[data-composer-submit]", 'form[data-chatgpt-composer] button[type="submit"]', "#composer-submit-button",
                 '[data-testid="send-button"]'],
        "stop": ['li[data-message-role="assistant"]:not([data-message-complete])',
                 'button[data-composer-submit][aria-label^="停止"]', 'button[data-composer-submit][aria-label^="Stop" i]',
                 '[data-testid="stop-button"]', 'form[data-chatgpt-composer] button[aria-label^="Stop" i]',
                 '[data-message-streaming="true"]', '[data-is-streaming="true"]', ".result-streaming"],
        "response": ['li[data-message-role="assistant"] [data-assistant-markdown-root]',
                     '[data-message-author-role="assistant"]', '[data-markdown-text-style="assistant-message"]',
                     '[class*="MarkdownRoot-"]'],
        "exclude": ['[data-testid="chatgpt-citation"]', '[data-markdown-copy="exclude"]', "[data-code-block-controls]",
                    ".turn-action-controls", ".sr-only", "h4[data-conversation-role]"],
        "scroll": ["[data-app-action-timeline-scroll]", "[data-scroll-root]"],
        "sidebar_close": ['button[aria-label="关闭边栏"]', 'button[aria-label="显示/隐藏侧边栏"]', 'button[aria-label="Close sidebar"]'],
        "sidebar": ["aside#ds-sidebar", '[data-testid="sidebar"]', "aside"],
        "incognito_url": "https://chatgpt.com/?temporary-chat=true",
    },
    "claude": {
        "updated": "2026-10",
        "options": [
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["Opus", "Sonnet", "Haiku"],
             "trigger": ['[data-testid="model-selector-dropdown"]'],
             "items": ['[role="menuitemradio"]', '[role="menuitem"]'], "note": "未实测"},
        ],
        "note": "来源：gemini-voyager、Claude2Markdown、claude-ai-chat-exporter。无痕网址未见官方说明，失效时改用页面右上角的无痕按钮",
        "input": ['[data-testid="chat-input"][contenteditable="true"]', 'div.ProseMirror[contenteditable="true"]'],
        "send": ['[data-testid="chat-input-send"]', 'fieldset button[aria-label^="Send" i]'],
        "stop": ['[data-testid="assistant-message"][data-is-streaming="true"]', '[data-is-streaming="true"]',
                 '[data-perf-row-streaming="true"]', 'fieldset button[aria-label^="Stop" i]'],
        "response": ['[data-testid="assistant-message"]', '[data-cds="AssistantMessage"]', ".font-claude-response",
                     ".font-claude-message"],
        "exclude": ['[data-cds="TurnStatus"]', '[data-cds="TurnStatusStep"]', "button[data-cds-row-toggle]",
                    '[data-testid="thinking-block"]', '[data-cds="MessageActions"]',
                    '[role="group"][aria-label="Message actions"]', '[data-testid="message-warning"]', ".sr-only"],
        "scroll": ['[data-autoscroll-container="true"]', '[data-testid="transcript-list"]'],
        "incognito_url": "https://claude.ai/new?incognito",
    },
    "gemini": {
        "updated": "2026-10",
        "options": [
            {"id": "mode", "label": "模式", "kind": "radio", "choices": ["对话", "任务"],
             "items": ['[role="group"] button'], "note": "2026-10-30 实测登录后：输入框下方“对话/任务”，任务模式只能选 3.8 Flash、4 Argon"},
            {"id": "model", "label": "模型", "kind": "menu",
             "trigger": ['[data-test-id="bard-mode-menu-button"]'], "items": ["gem-menu-item[data-mode-id]"],
             "choices_by": {"on": "mode", "map": {"对话": ["3.5 Flash-Lite", "3.8 Flash", "3.1 Pro", "4 Argon"],
                                                 "任务": ["3.8 Flash", "4 Argon"]}}},
            {"id": "think", "label": "思考等级", "kind": "menu", "choices": ["低", "中", "高"],
             "trigger": ['[data-test-id="bard-mode-menu-button"]'], "submenu": ['gem-menu-item[value="thinking_level"]', '[value="thinking_level"]'],
             "items": ["gem-menu-item:not([data-mode-id]):not([value])"], "note": "在模型菜单的“思考等级”里：低（快速高效）/ 中 / 高（超级详尽）"},
            {"id": "tool", "label": "工具", "kind": "menu", "choices": ["Deep Research", "学习辅导"],
             "trigger": ['button[aria-label="上传和工具"]', 'button[aria-label*="tools" i]'], "submenu": ['[data-test-id="more-tools-button"]'],
             "items": ["gem-menu-item", '[role="menuitem"]', '[role="menuitemcheckbox"]'], "note": "对话模式下“上传和工具 → 更多工具”"},
        ],
        "note": "来源：gemini-voyager、ai-chat-to-markdown、gemini-auto-temp-chat。思考程度在模式菜单里，不是独立开关",
        "input": ['rich-textarea [contenteditable="true"]', 'div.ql-editor[contenteditable="true"]'],
        "send": [".send-button.submit button", "button.send-button", 'button[aria-label*="Send" i]',
                 'button[aria-label*="发送"]'],
        "stop": ['button[data-test-id="stop-button"]', 'button[aria-label="Stop response"]',
                 'button[aria-label*="停止"]'],
        "response": ["model-response message-content .markdown", ".markdown-main-panel", "model-response message-content"],
        "exclude": ["model-thoughts", ".thoughts-container", ".thoughts-header", "sources-carousel-inline",
                    "source-inline-chips", "source-inline-chip", ".source-inline-chip-container", "sources-list",
                    "message-actions", ".response-footer", ".code-block-decoration", ".cdk-visually-hidden"],
        "scroll": ["infinite-scroller.chat-history", "#chat-history", '[data-test-id="chat-history-container"]'],
        "sidebar_close": ['button[aria-label="关闭边栏"]', 'button[aria-label="Close sidebar" i]', 'button[aria-label="Main menu"]'],
        "sidebar": ["bard-sidenav"],
        "incognito_button": "临时对话|临时聊天|Temporary chat",
    },
    "deepseek": {
        "updated": "2026-10",
        "options": [
            {"id": "think", "label": "深度思考", "kind": "toggle", "selector": [".ds-toggle-button", 'div[role="button"]'], "text": "^深度思考$|^DeepThink$",
             "on_class": "ds-toggle-button--selected"},
            {"id": "search", "label": "智能搜索", "kind": "toggle", "selector": [".ds-toggle-button", 'div[role="button"]'], "text": "^智能搜索$|^联网搜索$|^Search$",
             "on_class": "ds-toggle-button--selected"},
        ],
        "note": "思考过程也是 .ds-markdown，靠排除 .ds-think-content 去掉。对话列表是虚拟列表。"
                "来源：geo-data-engine、multi-ai-chat、DeepSeek_geo_collector、DeepSeek-Chat-Exporter",
        "input": ["textarea#chat-input", 'textarea[placeholder*="DeepSeek"]', "textarea.ds-scroll-area"],
        "send": ['[data-testid="chat-input-send-button"]',
                 'div[role="button"].ds-button--primary.ds-button--circle:not(.ds-button--disabled)',
                 "button.ds-button--primary:not(.ds-button--disabled)"],
        "stop": [],
        "response": [".ds-markdown.ds-assistant-message-main-content", ".ds-message .ds-markdown"],
        "exclude": [".ds-think-content", ".ds-markdown-cite", ".ds-message-feedback-container",
                    '[class*="message-actions"]'],
        "scroll": [".ds-virtual-list.ds-scroll-area"],
        "mode_reasoning": "深度思考|DeepThink",
        "mode_search": "智能搜索|联网搜索|Search",
    },
    "kimi": {
        "updated": "2026-10",
        "options": [
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["K3.1 Pro", "K2.8 Preview", "快速"],
             "trigger": ['[data-testid="model-select-trigger"]'], "items": ['[data-testid="model-option"]', '[role="menuitemradio"]'],
             "note": "2026-10-30 实测。“快速”模型没有思考强度"},
            {"id": "more", "label": "更多模型", "kind": "menu", "choices": ["K3"],
             "trigger": ['[data-testid="model-select-trigger"]'], "submenu": ['[data-testid="model-more-trigger"]'],
             "items": ['[data-testid="model-more-option"]', '[role="menuitemradio"]'], "note": "和“模型”二选一"},
            {"id": "effort", "label": "思考强度", "kind": "menu", "choices": ["标准", "进阶", "极致"],
             "trigger": ['[data-testid="model-select-trigger"]'], "submenu": ['[data-testid="model-effort-item"]'],
             "items": ['[data-testid="model-effort-option"]', '[role="menuitemradio"]'], "note": "极致消耗额度更快"},
        ],
        "note": "思考模式在模型下拉里选（如“K2.6 思考”），会自动联网。来源：geo-data-engine、gemini-voyager、opencli、Panelize",
        "input": ['.chat-input-editor[contenteditable="true"]', '[data-lexical-editor="true"]'],
        "send": [".send-button-container:not(.disabled)"],
        "stop": [".send-button-container.stop", 'svg[name="Stop"]'],
        "response": [".chat-content-item-assistant .markdown-container:not(.toolcall-content-text) .markdown",
                     ".segment-assistant .markdown", ".chat-content-item-assistant .markdown"],
        "exclude": [".toolcall-container", ".toolcall-content-text", ".thinking-container",
                    '[class*="upgrade-membership"]'],
        "scroll": [".chat-detail-main", ".chat-content-list"],
        "sidebar_close": ['[data-testid="sidebar-collapse-trigger"]', '[aria-label="收起导航"]'],
        "mode_reasoning": "思考|长思考|Thinking",
    },
    "doubao": {
        "updated": "2026-10",
        "options": [
            {"id": "mode", "label": "模式", "kind": "radio", "choices": ["对话", "工作"],
             "items": ['[role="tab"]', "button", 'div[class*="tab" i]'], "note": "2026-10-30 实测登录后：顶部“对话/工作”"},
            {"id": "think", "label": "思考", "kind": "menu", "choices": ["快速", "专家"],
             "trigger": ['[aria-haspopup="menu"]'], "trigger_text": "^(快速|专家)$",
             "items": ['[role="menuitem"]', '[role="menuitemradio"]', "[data-radix-popper-content-wrapper] div", '[role="menu"] div'],
             "note": "对话模式下输入框里的“快速/专家”"},
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["自动", "豆包 2.1 Lite", "豆包 2.1 Pro"],
             "trigger": ['[aria-haspopup="menu"]'], "trigger_text": "自动|豆包|Lite|Pro",
             "items": ['[role="menuitem"]', '[role="menuitemradio"]', "[data-radix-popper-content-wrapper] div", '[role="menu"] div'],
             "note": "工作模式下的模型；豆包 2.1 Pro 需要会员，免费账号选不上"},
            {"id": "effort", "label": "推理强度", "kind": "menu", "choices": ["低", "中", "高"],
             "trigger": ['[aria-haspopup="menu"]'], "trigger_text": "自动|豆包|Lite|Pro",
             "submenu": ['[role="menuitem"]', "[data-radix-popper-content-wrapper] div"], "submenu_text": "^推理强度",
             "items": ["[data-radix-popper-content-wrapper] div", '[role="menuitem"]'], "note": "工作模式下，模型菜单里的“推理强度”"},
        ],
        "note": "2026 年输入框换成 Tiptap，回车只换行，必须点发送按钮；会自动联网。"
                "来源：geo-data-engine、multi-ai-chat、Better_Doubao、long-conversation-export",
        "input": ['.tiptap.ProseMirror[contenteditable="true"]', '[data-testid="chat_input"] .ProseMirror',
                  'textarea[data-testid="chat_input_input"]'],
        "send": ['[data-testid="chat_input_send_button"]', "button#flow-end-msg-send", ".send-btn-wrapper button"],
        "submit": "button",
        "stop": ['[data-streaming="true"]'],
        "response": ['[data-testid="receive_message"] [data-testid="message_text_content"]',
                     "[data-message-id] .flow-markdown-body", '[data-message-id] [class*="md-box-root"]'],
        "exclude": ['[data-plugin-identifier*="block_type:10040"]', '[class*="think-block"]', '[class*="action-bar"]',
                    '[class*="message-action"]'],
        "scroll": ['[class*="v_list_scroller"]', '[class*="message-list"]'],
        "sidebar_close": ['[data-testid="siderbar_close_btn"]', '[aria-label="收起侧栏"]'],
    },
    "qianwen": {
        "updated": "2026-10",
        "options": [
            {"id": "mode", "label": "模式", "kind": "menu", "choices": ["快速", "思考研究"],
             "trigger": ['button[aria-haspopup="menu"][aria-pressed]'],
             "items": ['[role="menu"] [role="menuitemcheckbox"]', '[role="menu"] [role="menuitem"]'],
             "note": "2026-10-30 实测：快速（适用于大多数情况）/ 思考研究（深度搜索、深度研究）"},
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["Qwen-快速", "Qwen3.7-Max"],
             "trigger": ['div[aria-haspopup="dialog"]'], "trigger_text": "Qwen",
             "items": ['[role="dialog"][data-state="open"] div']},
        ],
        "note": "回车发送或一次性粘贴可能被风控（回答“我无法回答这个问题”），所以逐字输入并点发送按钮；会自动联网。"
                "来源：geo-data-engine、opencli、Panelize",
        "input": ['[data-chat-input-body="true"] [data-slate-editor="true"][contenteditable="true"]',
                  'div[role="textbox"][contenteditable="true"][data-slate-editor="true"]'],
        "send": ['[data-chat-input-layout="true"] button[aria-label="发送消息"]', 'button[aria-label="发送消息"]'],
        "submit": "button",
        "typing": "keys",
        "risk_answers": ["我无法回答这个问题"],
        "stop": [],
        "response": ["[data-chat-answers-wrap] .qk-markdown", ".chat-round .qk-markdown", ".qk-markdown"],
        "exclude": [],
        "scroll": [],
        "sidebar_close": ['button[aria-label="收起侧边栏"]'],
        "sidebar": ["aside#new-nav-tab-wrapper"],
        "mode_reasoning": "深度思考",
    },
    "yuanbao": {
        "updated": "2026-10",
        "options": [
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["快速回答"],
             "trigger": ["button[data-thinking-mode-switcher-trigger]", 'button[aria-label="切换模型"]', '[dt-button-id="model_switch"]'],
             "items": ['[role="menuitemradio"]', '[role="menuitem"]'],
             "note": "未登录只看到“快速回答”；登录后菜单里的其他选项可以手填"},
            {"id": "think", "label": "深度思考", "kind": "toggle", "selector": ['[dt-button-id="deep_think"]'],
             "on_class": "ThinkSelector_selected__"},
            {"id": "search", "label": "联网搜索", "kind": "toggle", "selector": ['[dt-button-id="internet_search"]'],
             "on_attr": ["dt-internet-search", "openInternetSearch"]},
        ],
        "note": "思考过程和正文都是 .hyc-content-md，取最后一个即正文。来源：geo-data-engine、opencli、Panelize、ask-many",
        "input": ['.chat-command-editor-specail .ql-editor[contenteditable="true"]', 'div.ql-editor[contenteditable="true"]'],
        "send": ["#yuanbao-send-btn", 'a[class*="send-btn"]:not([class*="disabled"])'],
        "stop": ['[aria-label="停止回答"]', '[class*="sendStop"]'],
        "response": [".agent-chat__list__item--ai .hyc-content-md"],
        "exclude": [".agent-chat__toolbar"],
        "scroll": [".agent-chat__list__content", '[class*="agent-chat__list"]'],
        "mode_reasoning": "深度思考",
        "mode_search": "联网搜索",
        "incognito_button": "进入临时对话|临时对话",
        "dismiss": "我知道了|知道了",
        "sidebar_close": ['.yb-common-nav__trigger[data-desc="fold"]', '[aria-label="收起侧栏"]'],
        "sidebar": [".yb-nav__content-wrapper", '[class*="yb-nav__content"]'],
    },
    "wenxin": {
        "updated": "2026-10",
        "builtin": {"name": "文心助手", "url": "https://wenxin.baidu.com/"},
        "note": "文心一言网页版 2026-06 并入文心助手，可以不登录提问。来源：wenxin_geo_collector、wenxin-enhance-mobile、simple-chat-hub",
        "options": [
            {"id": "mode", "label": "模式", "kind": "menu", "choices": ["对话", "工作任务", "专业技能"],
             "trigger": ['[data-testid="chat-mode-selector"]', ".ci-input-mode-button"],
             "items": ['[role="menuitemradio"].ci-input-mode-item', '[role="menuitemradio"][data-chat-mode]'],
             "note": "2026-10-30 实测：对话（聊天问答）/ 工作任务 / 专业技能"},
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["自动", "文心 5.1"],
             "trigger": ['[data-testid="chat-mode-selector"]', ".ci-input-mode-button"],
             "submenu": ['[data-testid="chat-mode-model-row"]'], "items": ["[data-model-name]"]},
        ],
        "input": ["textarea#chat-textarea", "textarea.ci-textarea"],
        "send": ["span.ci-submit-button", "#ci-submit-button-ai"],
        "submit": "button",
        "stop": ['.cs-answer-container[data-status="GENERATING"]', '[class*="_capsule-loading"]'],
        "response": [".cs-answer-container .ai-entry-block.ai-markdown", ".cs-answer-container .ai-markdown", ".cs-answer-container"],
        "exclude": [".cosd-citation", '[aria-label*="参考文档"]'],
        "scroll": ['[class*="_chat-body-container"]', '[class*="_scroll-container"]', "#cs-container-scroll"],
        "sidebar_close": ['[data-testid="aside-outer-collapse-btn"]', ".aside-outer-collapse"],
        "sidebar": [".chat-aside:not(.collapsed)"],
    },
    "google_ai": {
        "updated": "2026-10",
        "builtin": {"name": "Google AI Mode", "url": "https://www.google.com/search?udm=50&hl=zh-CN"},
        "old_urls": ["https://www.google.com/search?udm=50&hl=en"],     # 旧版本存下的默认网址，自动换成新的
        "note": "问题直接放进网址发送，追问在结果页输入框里发。选了模型时先打开首页选好模型再输入提问。"
                "中国大陆不提供。回答区域的类名是混淆过的，改版后需要试跑更新。来源：AfterChat、Panelize",
        "query_url": "https://www.google.com/search?udm=50&hl=zh-CN&q={q}",
        "options": [
            {"id": "model", "label": "模型", "kind": "menu", "choices": ["快速", "3.8 Flash", "4 Argon"],
             "trigger": ['button[aria-label*="选择模型"]', 'button[aria-label*="select a model" i]', 'button[aria-label="添加文件和工具"]',
                         'button[aria-label="Add files and tools"]'],
             "items": ['[role="menuitemradio"]', '[role="menuitem"]'],
             "note": "实验性：Google 只对部分账号、部分页面提供模型选择，没有时按默认模型提问并在结果里标注"},
        ],
        "input": ["textarea.ITIRGe", 'textarea[aria-label="Ask anything"]', 'textarea[aria-label="有问必答"]', 'textarea[maxlength="8192"]'],
        "send": ['button[data-xid="input-plate-send-button"]'],
        "stop": ['button[aria-label="Stop"]', 'button[aria-label="停止"]'],
        "response": ["div.CKgc1d div.pWvJNd", "div.pWvJNd"],
        "exclude": [".DBd2Wb", ".RkJvxe", "span.WBgIic"],
        "scroll": [],
        "sidebar_close": ['button[aria-label="Close sidebar"]', 'button[aria-label="关闭边栏"]'],
    },
    "xhs_diandian": {
        "updated": "2026-10",
        "builtin": {"name": "小红书点点", "url": "https://www.xiaohongshu.com/ai_chat"},
        "note": "需要登录小红书账号；操作太快容易触发安全验证，建议把采集间隔调大。来源：geo-data-engine、opencli、simple-chat-hub",
        "options": [],
        "input": [".ai-chat-welcome__input textarea", ".input-container textarea"],
        "send": [".bottom-box-right-submit-button"],
        "typing": "keys",
        "stop": [],
        "response": [".ai-message"],
        "exclude": ["span.progress-text"],
        "scroll": [],
    },
}


def preset(key):
    return ADAPTERS.get(key) or {}


def preset_selectors(key):
    p = preset(key)
    return {k: list(p.get(k) or []) for k in SEL_KINDS}


def preset_text(key):
    """文字类默认值（不含选择器和元信息）。"""
    return {k: v for k, v in preset(key).items() if k not in SEL_KINDS and k not in ("updated", "note", "options", "builtin", "old_urls")}


def builtin_sites():
    """后加的内置产品（不在最初默认列表里），升级后自动出现在用户的产品列表里。"""
    return [{"key": k, **v["builtin"], "enabled": True} for k, v in ADAPTERS.items() if v.get("builtin")]


def preset_options(key):
    """该产品采集前可选的模型/模式。没有内置适配的用通用的深度思考/联网开关。"""
    p = preset(key)
    return [dict(o) for o in (p["options"] if "options" in p else GENERIC_OPTIONS)]


def preset_info(key):
    p = preset(key)
    if not p:
        return None
    return {"updated": p.get("updated", ""), "note": p.get("note", "")}
