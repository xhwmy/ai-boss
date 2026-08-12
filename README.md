# BOSS 直聘 AI 自动投递助手

> 自动搜索 BOSS 直聘岗位 → AI 分析 JD 匹配度 → 自动投递合适岗位

Windows · Python 3.10+ · DeepSeek AI · KIMI Webbridge 浏览器扩展

## ✨ 功能特性

- 🔍 **智能搜索**：多关键词搜索 + 网页原生筛选（经验/学历/薪资/类型）+ 左侧列表滚动懒加载累积岗位
- 🤖 **AI 匹配分析**：基于个人简历按可自定义评分维度加权评分（技术栈/经验/方向/薪资/工作制度等）
- 🎮 **两种运行模式**：全自动（AI 直接投递）/ 半自动（AI 分析后进审核列表，人工确认再投递）
- ⭐ **超预期岗位收藏**：匹配度 ≥ 超预期阈值的岗位自动入库，独立页面查看（含 BOSS 网址/公司规模/是否上市/行业）
- 🚫 **低质量岗位黑名单**：劳务外包 / 岗位名与JD不符 / 远超简历 自动分级拉黑（永久/短期/观察），重复遇到直接跳过，不再读 JD、不调 AI
- 📊 **Web 控制面板**：在线改配置、管理简历、实时日志、统计面板、人工审核
- 🛡️ **零自动化特征**：通过 KIMI Webbridge 浏览器扩展（chrome.debugger CDP 协议）控制浏览器，无法被反爬检测

> ⚠️ **免责声明**：本项目仅供学习研究使用。请遵守 BOSS 直聘的用户协议与当地法律法规，控制投递频率（默认 3-8 秒随机间隔、每小时/每日上限），因使用本工具造成的一切后果由使用者自行承担。

## 🛠️ 技术栈

| 组件 | 说明 |
|------|------|
| Python | 3.10+（开发环境 3.14） |
| AI 模型 | DeepSeek Chat（`langchain-deepseek`） |
| Web 框架 | Flask（控制面板） |
| 浏览器控制 | KIMI Webbridge 扩展（chrome.debugger / CDP，WebSocket 协议） |
| 数据库 | SQLite（超预期收藏 + 黑名单） |

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone <你的仓库地址>
cd boss自动投递
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 准备配置文件

复制示例模板（Windows / macOS / Linux）：

```bash
copy config.example.json config.json        # macOS/Linux: cp config.example.json config.json
copy resume.example.json resume.json        # macOS/Linux: cp resume.example.json resume.json
copy .env.example .env                      # macOS/Linux: cp .env.example .env
```

### 4. 填写配置

- **`.env`**：填入你的 DeepSeek API Key（到 [platform.deepseek.com](https://platform.deepseek.com/) 注册获取）
- **`config.json`**：搜索关键词、意向城市、匹配阈值、评分维度、黑名单策略等
- **`resume.json`**：你的个人信息、技能、实习/项目经历、期望薪资（AI 评分依据）

> 📝 也可以在控制面板网页中在线修改这些配置，无需手编 JSON。

### 5. 启动浏览器扩展

1. 安装 [KIMI Webbridge](https://chromewebstore.google.com/) 浏览器扩展（`chrome.debugger` API 控制浏览器）
2. 打开 Edge / Chrome 浏览器，保持扩展启用
3. 脚本通过 WebSocket（默认 `ws://127.0.0.1:10086/ws`）与扩展通信，扩展每 30 秒自动重连

### 6. 运行

**方式一：Web 控制面板（推荐）**

```bash
python app.py
```

浏览器打开 `http://localhost:5000`，点击「启动」开始。首次使用建议用**半自动模式**观察 AI 判断质量。

**方式二：命令行 CLI**

```bash
python main.py
```

### 7. 登录

- 脚本自动检测登录状态，未登录时会在浏览器打开 BOSS 直聘登录页
- 用手机 BOSS 直聘 App 扫码登录
- 之后保持浏览器打开，扩展持续连接

## 🖥️ Web 控制面板

| 功能 | 说明 |
|------|------|
| 🎮 启动/暂停/停止 | 控制引擎运行 |
| 🔧 在线改配置 | 关键词、城市、阈值、网页筛选条件一键保存 |
| 🎯 评分维度 | 增删改评分维度（名称/权重/说明/启用），AI 立即生效 |
| 📝 简历管理 | 技能/实习/项目/证书可视化维护 |
| 👤 半自动模式 | AI 搜索入审核列表，人工决定投递/跳过/拉黑 |
| 🤖 全自动模式 | AI 直接决定并投递 |
| 📜 实时日志 | 浏览器实时查看运行状态 |
| ⭐ 超预期岗位 | 独立页查看收藏（`/starred`） |
| 🚫 黑名单 | 独立页查看/管理拉黑（`/blacklist`） |

## ⚙️ 主要配置项

| 配置 | 说明 | 默认 |
|------|------|------|
| `search.keyword` / `search.city` | 搜索关键词 / 意向城市 | AI应用开发 / 空 |
| `ai.match_threshold` | 匹配度阈值（≥ 投递） | 60 |
| `ai.over_match_threshold` | 超预期阈值（≥ 收藏） | 80 |
| `scoring.dimensions` | 评分维度（名称/权重/说明/启用） | 5 个默认维度 |
| `application.max_per_hour` / `daily_limit` | 每小时 / 每日投递上限 | 15 / 50 |
| `application.delay_range_seconds` | 操作间隔（模拟人类） | [3, 8] |
| `blacklist.enabled` | 低质岗位自动拉黑 | true |

完整配置说明见 `CLAUDE.md`。

## 📁 项目结构

```
boss自动投递/
├── app.py              # Web 控制面板服务器
├── main.py             # CLI 命令行版
├── boss_engine.py      # 后台引擎（启动/暂停/停止/人工干预）
├── browser.py          # 浏览器自动化（登录/搜索/投递/信息提取）
├── analyzer.py         # AI 分析模块（JD 匹配评分 + 岗位质量判断）
├── blacklist.py        # 低质量岗位拉黑判定策略
├── database.py         # SQLite（超预期收藏 + 黑名单）
├── config.py           # 配置读取/校验
├── webbridge_client.py # KIMI Webbridge WebSocket 客户端
├── utils.py            # 工具函数（延时/日志/结果保存）
├── config.example.json # 配置示例模板
├── resume.example.json # 简历示例模板
├── .env.example        # API Key 示例
├── templates/          # 控制面板页面
└── results/ · logs/ · data/   # 运行产物（自动生成）
```

## 📄 License

本项目基于 [GPL-3.0](LICENSE) 协议开源。
