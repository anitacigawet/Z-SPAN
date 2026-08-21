<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN 属于所有人。一座关于地方政治的虚拟图书馆。由人们维护，为人们服务。" width="1000">
</p>

> *Scientia potentia est.*
>
> **知识就是力量。**
>
> — 弗朗西斯·培根

---

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [**简体中文**](README.zh-CN.md) · [繁體中文](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**一座关于地方政治的虚拟图书馆。**

[前往 zspan.org 访问 Z-SPAN](https://zspan.org)

✨ **完整公开，人人可用。借助每个人的力量不断扩展。**

Z-SPAN 希望让地方公共会议更容易查找、观看和理解。一个地方就是一个频道，
一场会议就是一集节目，而原始视频、议程和会议记录始终保留在查阅路径中。

这个代码仓库包含正在运行的图书馆本身：网站、公共 API、会议来源解析器、
处理流程、本地客户端，以及确保生成内容始终以公共记录为依据的检查机制。
公开整套系统的原因很简单：由一个人维护的图书馆会随着那个人而结束；
一个能由其他人检查、运行、质疑并继续推进的图书馆则不会。

政府会议来源目录单独保存在
[National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog)
中。该仓库包含持续可用的公共端点及其证据，不包含 Z-SPAN 解析器、文字记录、
摘要或已处理的会议。Z-SPAN 只是这些数据可以支持的一种应用。

## 观看完整介绍

[![观看“Z-SPAN Is Born”——Z-SPAN 项目完整介绍](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) 从维护者的
视角介绍最初的亚利桑那图书馆。视频展示了 Z-SPAN 最初的构想、各部分如何协作，
以及这条公共道路希望由人们继续带向何处。

## 🗺️ 一个逐地建设的全国目录

亚利桑那是 Z-SPAN 目前处理并公开发布的概念验证。频道目录也为每个州和领地
提供真实的起始结构，按照州、县级等同区域、部落、区域和地方公共机构组织。

绿色书架已有 Z-SPAN 发布的会议。琥珀色书架如实表示工作仍在进行：这个地方
已经进入目录，但其持续会议来源或 Z-SPAN 解析器仍需完善。任何人都不必等待
邀请，就可以帮助自己的社区。

## 🐈 帮助你的家乡

1. 在 [zspan.org](https://zspan.org) 找到你的州和所在地。
2. 如果书架仍在等待，请点击那只睡觉的猫。
3. 将简短的 Markdown 交接说明复制到你已经在使用的 AI 助手中。
4. 回答几个关于该地点及其官方会议页面的普通问题。你不需要了解 JSON 或 Git。
5. 如果可以使用 GitHub 工具，助手可以准备一份范围明确的 pull request 供你确认；
   如果不能，它会改为准备一份完整报告，提交到简单的 GitHub 表单。

贡献会进入 National Civics Catalog，由可信检查程序和一位真人审核端点及其证据。
它绝不会直接发布到 Z-SPAN。

**Z-SPAN 的三天承诺：**目录贡献被接受后，Z-SPAN 会在三天内创建相应解析器，
或发布一项清楚可见的“来源受阻”结果。这项承诺是为了让来源变得可用，或诚实
说明它为何暂时无法使用；并不意味着自动发布由 AI 生成的会议内容。

[阅读 AI 贡献说明](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 为什么要建立这座图书馆

处理地方公共记录的项目往往会遇到同样的问题：

- 当政府网站采用不同的组织方式时，人们应该如何浏览会议？
- 面对不同地点和视频服务商，一个界面如何始终保持实用？
- 如何让返回官方来源的路径始终清楚可见？
- 技术系统如何解释自身，而不迫使人们阅读底层数据库？

Z-SPAN 是一个可行的答案，但不是唯一答案。这个仓库的目标是让整个项目保持
可见，让使用它的人能够检查、质疑并把它带得更远。

## 👋 这座图书馆适合谁

无论你是学生、行动者、记者、研究人员、设计师、开发者、志愿者，还是只是对
地方公共信息感到好奇，都不需要采用整个项目才能从中找到有用的东西。图书馆
的组织方式让人可以一次理解一个想法或组件，也可以一次添加一个地方。

## 🗂️ 这个仓库如何组织

- [council_navigator](../02_Core_Project/council_navigator/) — 网站、公共 API、
  本地会议缓存和公共频道目录。
- [parsers](../02_Core_Project/council_navigator/parsers/) — 针对具体来源的日历
  解析器，将目录端点转换成统一的会议结构。
- [zspan_pipeline](../02_Core_Project/zspan_pipeline/) — 处理队列，将会议录像
  转换成有依据、可审核的材料。
- [zspan_cli](../02_Core_Project/zspan_cli/) — 让人们在自己的电脑和工作区中使用
  Z-SPAN 的本地客户端。
- [prompts](../02_Core_Project/prompts/) — 处理流程采用的已公开综合约定。

National Civics Catalog 保持为独立仓库，这样人们可以改进来源目录而不改变
Z-SPAN 应用，其他项目也能出于完全不同的目的使用同一批端点。

## 本项目坚持什么

以下是项目为自己设定的约束，而不只是愿望：

- **不对公职人员作倾向性评论。** 他们的话按原文呈现，注明说话者和来源。
  判断由你作出。
- **不汇总普通公民的数据。** 本项目关注的是履行公共职责的官员；不会为在
  公共麦克风前发言的居民建立档案。
- **阅读永远不设门槛。** 阅读已发布的公共记录内容不需要付费、订阅、登录或注册。
- **不优化参与度。** 没有无限信息流、推荐算法或煽动愤怒的机制。记录有意保持平静。
- **任何内容发布前都由真人审核。** 处理可以自动化，发布不能。
- **按设计用于非商业用途。** 许可证把这条边界写进了结构中。

## 🏛️ 初创维护

Z-SPAN 始于亚利桑那，由
[@anitacigawet](https://github.com/anitacigawet) 维护。对来源目录的贡献会在
National Civics Catalog 中获得署名；Z-SPAN 的实现则继续在这里单独审核和维护。

## ⚖️ 许可证

已发布代码采用
[PolyForm Noncommercial License 1.0.0](../LICENSE)。在遵守许可证条款的前提下，
可出于非商业目的研究、修改、分享和重复使用，包括个人学习、兴趣项目、教育、
公共研究、慈善工作和政府用途。

该许可证不授予商业使用权。必须保留的声明以及 Z-SPAN 名称的使用边界记录在
[NOTICE](../NOTICE) 中。

## 联系方式

项目托管于 [zspan.org](https://zspan.org)。欢迎通过本仓库的
[问题跟踪器](https://github.com/anitacigawet/Z-SPAN/issues) 提交问题和可复现的错误报告。

---

## Z-SPAN 三位一体

![Z-SPAN 三位一体：互联网负责传递，公民记录提供根基，人们让它持续存在](../repository-assets/zspan-trinity.svg)

---

> CIA、NSA，甚至五角大楼，都受限于其中工作人员有限的任期。
>
> **Z-SPAN 不受此限。**
>
> Z-SPAN 由人民推动、为人民服务，因此需要社区的充分参与和透明运作。
>
> — Z-SPAN 维护者

---

## 🌌 把这个想法带得更远

National Civics Catalog 按州组织，使来源目录能够扩展到全美国，而不要求任何人
采用 Z-SPAN 的界面或处理方式。你可以使用这些端点构建社区日历、研究工具、
无障碍项目、课堂资源，或任何这里尚无人想到的东西。

这个想法的价值并不来自它属于某一个应用，而来自人们可以不断找到新方法，
让公共记录更容易触达。
