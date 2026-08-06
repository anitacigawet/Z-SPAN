[本文暂由 AI 起草，最晚将于 2026 年 8 月 4 日重写]

# Z-SPAN

[English](README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [**简体中文**](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**一座关于地方政治的虚拟图书馆。**

[访问 Z-SPAN：zspan.org](https://zspan.org)

✨ **本项目公开发布，供人查阅、保存和借鉴。**

Z-SPAN 尝试让地方政府的公开会议更容易找到、观看和理解。每个地点成为一个频道，每场会议成为一期节目，而原始视频、议程和会议记录始终保留在查阅路径中。

这个代码仓库是“图书馆背后的图书馆”：它整理并公开一部分源代码、项目方法和经验，希望能帮助正在思考如何在其他城市、州或国家开展类似项目的人。

它不是生产系统的完整副本。多数书架提供的是精选参考材料：一种导航思路、一条播放边界、一种让原始材料始终可见的方法，或一项可以用于其他独立项目的设计原则。[`respawn-kernel/`](respawn-kernel/) 是有意设置的例外：它是一个可独立运行、不预设国家结构的公共会议图书馆起点。

> 本页是英文 README 的 AI 辅助翻译，欢迎能熟练使用简体中文的读者通过 pull request 提出修正。若不同语言版本之间存在含义差异，请以 [英文 README](README.md)、[LICENSE](LICENSE) 和 [NOTICE](NOTICE) 为准。所链接的其他文档目前仍为英文。

---

## 力量属于人民

> The CIA, the NSA, and even the Pentagon are bounded by the finite tenure of the humans who staff them.
>
> **Z-Span is not.**
>
> Z-Span is powered by the people, for the people, and thus requires full community involvement and transparency.
>
> If you would like to operate this library for your own country, here is how.
>
> — Z-SPAN operator

> **引语的简体中文译文：** 中央情报局、国家安全局，甚至五角大楼，都受限于其中工作人员有限的任期。Z-SPAN 不受这种限制。Z-SPAN 由人民推动、为人民服务，因此需要整个社区的充分参与和透明运作。如果你希望为自己的国家运营这座图书馆，方法就在这里。

从 [Respawn Kernel](respawn-kernel/README.md) 开始。输入一个国家、一个由当地选择的项目名称和主要语言，它就会创建一个独立仓库，其中包含不预设国家结构的数据协议、递归行政区模型、翻译支持、验证工具和一座可独立运行的公共图书馆。

任何国家，无一例外。从美利坚合众国到中国。每个独立项目都拥有自己的名称、来源、决定、发布选择和相应责任。完整步骤见[国家启动指南](respawn-kernel/BOOTSTRAP.md)。

## 📚 为什么要建立这座图书馆

处理地方公共记录的项目通常都会遇到相似的问题：

- 当不同政府网站采用不同的组织方式时，人们应该如何浏览会议？
- 面对不同城市和视频平台，一个界面如何始终保持实用？
- 如何让返回官方来源的路径始终清楚可见？
- 技术系统如何解释自身，而不让人们去阅读其底层数据库？

Z-SPAN 是一种正在实践中的答案，但不是唯一答案。这个仓库的目标，是让其中有用的想法保持可见，以便其他项目能够审视、质疑并进一步运用这些想法。

## 👋 这座图书馆适合谁

无论你是学生、社会活动人士、记者、研究人员、设计师、开发者、志愿者，还是仅仅对地方公共信息感到好奇，你都不必采用整个项目，仍然可以从这里找到有用的内容。图书馆的组织方式让人可以一次理解一个想法或组件。

## 🧭 如何使用这个仓库

这里没有强制的阅读顺序，但以下入口会很有帮助：

1. 阅读[项目模型](docs/PROJECT_MODEL.md)，了解各部分如何关联的最简说明。
2. 打开[图书馆目录](CATALOG.md)，按照你想探索的问题选择代码、提示词或设计资料。
3. 浏览[值得带到其他项目中的设计方法](docs/DESIGN_PATTERNS.md)，了解界面背后的思路。
4. 使用[仓库导览](docs/REPOSITORY_GUIDE.md)，沿着一条具体的访客路径阅读已公开的源代码。
5. 在对更大的 Z-SPAN 系统作出判断前，先查看[哪些内容已公开、哪些没有](PUBLICATION_SCOPE.md)。
6. 使用 [Respawn Kernel](respawn-kernel/README.md) 开始建立一座独立的国家级图书馆。
7. 查看 [Respawn 发布快照](docs/snapshots/2026-08-05-respawn-kernel.md)，了解可运行版本的准确范围和审查状态。

## 🗂️ 书架上有什么

目前公开的源代码展示了访客体验和图书馆建设的七个部分：

- **查找地点或会议**：通过首页、频道、城市和搜索页面进入。
- **浏览现有内容**：通过可在卡片、地图、内嵌播放器和大屏观看模式之间切换的导览。
- **返回原始公共记录**：在官方视频、议程和会议记录可用时提供清晰链接。
- **通过统一界面播放视频**：即使底层视频托管平台不同，访客的操作方式仍保持一致。
- **向访客解释完整性检查**：通过审计、扫描和验证页面展示检查结果。
- **将会议记录整理成易读的公共事务摘要**：通过提示词书架上保留的三个经审查示例。
- **启动一座独立的国家级图书馆**：通过 Respawn Kernel、递归行政区协议、语言包和验证工具。

[此处将加入未来的视觉展示]

[仓库导览](docs/REPOSITORY_GUIDE.md)会把这些想法分别链接到相关文件。

## 关于运行代码

精选的访客界面源代码不是独立应用，其中不包含启动亚利桑那正式系统所需的私有服务、应用连接方式或生产配置。

Respawn 不同：它可以使用 Python 3 直接运行，创建新的国家仓库，验证仓库结构，并构建多语言图书馆预览。完整命令见 [Respawn README](respawn-kernel/README.md)。

## 仓库的组织方式

- [`docs/`](docs/) 说明项目模型、可复用方法、阅读路径和带日期的公开快照。
- [`code/`](code/) 存放经选择后公开的访客界面参考代码，并与私有工作项目中的路径分开组织。
- [`prompts/`](prompts/) 存放三个经审查且保持原样的提示词示例，可逐一研究或调整。
- [`respawn-kernel/`](respawn-kernel/) 是可运行的国家级图书馆起点。
- [`CATALOG.md`](CATALOG.md) 是面向人类读者和 AI 读者、按书架编排的索引。
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) 用清楚的语言说明公开边界。

公开导出只改变书架名称。`code/visitor-interface/src/` 内部的相对结构保持不变，因此页面、组件、播放器适配器和样式之间的关系仍然清晰可读。

## ⚖️ 许可证

公开代码采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。在遵守许可证条款的前提下，可以出于非商业目的研究、改编、分享和再利用这些代码，包括个人学习、兴趣项目、教育、公共研究、慈善工作和政府用途。

该许可证不授予商业使用权。必须保留的署名要求以及 Z-SPAN 名称的使用边界记录在 [NOTICE](NOTICE) 中。

## 联系方式

项目托管于 [zspan.org](https://zspan.org)。如果你有兴趣申请 Z-SPAN 生态中的开放席位，请发送邮件至 [anitacigawet@pm.me](mailto:anitacigawet@pm.me) 了解详情。
