<p align="center">
  <a href="https://pub-e5158abb37d74611a90c9a80bcd9fd9b.r2.dev/bettercallgpt/launch-video/2026-09-29/better-call-gpt-A-r8-16x9.mp4"><img alt="Better Call GPT：给 Claude Code 打个语音电话。点击观看 83 秒演示视频。" src="docs/assets/cover.jpg" width="860"></a>
</p>

<h1 align="center">Better Call GPT</h1>
<p align="center"><strong>给 Claude Code 打个语音电话。</strong>它写代码的时候你直接说，GPT Realtime 会回你。<br>全双工，随时能打断；口头说“同意”批准不了任何操作。</p>

<p align="center">
  <a href="#一次安装"><strong>安装</strong></a>
  &nbsp;&bull;&nbsp;
  <a href="#通话是什么样的"><strong>怎么用</strong></a>
  &nbsp;&bull;&nbsp;
  <a href="#安全设计"><strong>安全</strong></a>
  &nbsp;&bull;&nbsp;
  <a href="./README.md"><strong>English</strong></a>
</p>

<p align="center"><a href="https://pub-e5158abb37d74611a90c9a80bcd9fd9b.r2.dev/bettercallgpt/launch-video/2026-09-29/better-call-gpt-A-r8-16x9.mp4"><strong>▶ 观看 83 秒演示视频</strong></a></p>

<p align="center">
  <img alt="你对 GPT Realtime 说话，它把真正的请求转给你的 Claude Code 会话，再把结果读给你听" src="docs/assets/how-it-works.svg" width="860">
</p>

## 一次安装

```bash
npx skills add insta-fusion/bettercallgpt -g      # 需要 Node；macOS + Claude Code
```

然后对你的 AI 说：**“帮我装好 bettercallgpt”**。没装 Node？直接把这句贴给 Claude Code：`Set up https://github.com/insta-fusion/bettercallgpt for me.`（它会照 [AGENTS.md](AGENTS.md) 来装）。它会检查 [uv](https://docs.astral.sh/uv/) 和你的系统、装好
Claude Code 插件、建一个空的 key 文件、再跑一遍自检。你自己只做两件事：

1. **把 key 粘进它告诉你的那个文件**（Azure Voice Live；OpenAI Realtime 还在实验阶段）。安装过程不会问你要 key——也别把 key 发到聊天里。
2. **在新开的 Claude Code 会话里输入 `/bettercallgpt:on`**，批准一次启动。听到上扬的提示音，就通上了。

bettercallgpt 免费、MIT 开源；语音服务的费用走你自己的账户。不想用 skill？先装好 [uv](https://docs.astral.sh/uv/)，再输入
`/plugin marketplace add insta-fusion/bettercallgpt` 和 `/plugin install bettercallgpt@bettercallgpt`，
照着 [`.env.example`](.env.example) 填好 `~/.config/bettercallgpt/.env`，再 `/bettercallgpt:on`。

## 通话是什么样的

- **你说，它干活。** 边想边说就行；你的话会以带 `⟨v#…⟩` 标记的一行进入会话，结果出来时读给你听。
- **随时打断。** 全双工，能插话：不用按住说话，也不用对讲机式轮流。干到一半可以问“进度怎么样？”
- **闲聊留在语音里。** 只有真正的请求才会交给 AI。
- **会自己结束。** 说一声“好了”、输入 `/bettercallgpt:off`，或者安静 10 分钟（AI 还在干活时不会挂）。下降的提示音表示结束。

| 命令 | 作用 |
|---|---|
| `/bettercallgpt:on` | 在当前会话开始通话 |
| `/bettercallgpt:status` | 一行状态：阶段、连接 |
| `/bettercallgpt:off` | 结束通话 |

## 安全设计

- **口头说“同意”批准不了任何操作。** 语音回答不了权限确认，只能你在键盘上回答。（AI 本来就被允许做的事，仍由你的 Claude Code 权限设置决定。）
- **启动前会先问你**——除非 Claude Code 处于 auto / bypass 模式，或有匹配的白名单规则。别把它加进白名单（不要 `bettercallgpt` 通配，也不要宽泛的 `uvx` 规则）：它会打开麦克风和付费连接。
- **什么会离开你的电脑：** 你的麦克风音频，以及语音需要用来聊工作的内容（你的提示、AI 的进度和结果、权限提示），会发给你配置的语音服务。通话记录留在本地（`0600`）。详见 [SECURITY.md](SECURITY.md#privacy-notes)。
- **只接入启动它的那个会话**，无需按键即可证明归属，其他一律拒绝。
- **插件就是三个小命令文件**（[plugin/commands/](plugin/commands/)），只有你能运行：没有 hook，语音进程只在通话期间运行。它们通过 `uvx` 运行 GitHub 上打了标签的发布版本；发布版本用不可更改的 GitHub release，标签发布后不能再改指。

## 支持范围

| | 现在 | 还没有 |
|---|---|---|
| AI | **macOS 上的 Claude Code CLI**（实测可用），任意终端 | Claude 桌面版 Code 标签（未验证）、Cowork（跑在虚拟机里：不支持）、Codex 作为子进程（`VOICE_BACKEND=process`，仅单元测试） |
| 语音 | **Azure Voice Live**（默认，实测可用，自带回声消除：外放也行） | OpenAI Realtime（仅单元测试，无回声消除：需戴耳机）；Azure GPT-Live（短通话实测，需戴耳机） |
| 系统 | **macOS** | Linux / Windows：Claude Code 后端的进程归属校验目前只支持 macOS |
| Orca | 在任意 [Orca](https://github.com/stablyai/orca) 窗格里都能用；窗格能证明归属时（需要 `orca` 命令），Claude 等你批准时语音会提醒你 | 这种模式下的长通话：未验证 |

配置项、手动启动、架构和测试见 [English README](README.md#configuration)。
