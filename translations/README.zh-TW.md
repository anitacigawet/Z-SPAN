[本文暫由 AI 起草，最遲將於 2026 年 8 月 4 日重寫]

# Z-SPAN

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [**繁體中文**](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**一座關於地方政治的虛擬圖書館。**

[前往 zspan.org 造訪 Z-SPAN](https://zspan.org)

✨ **本專案公開發布，供人查閱、保存與借鑑。**

Z-SPAN 嘗試讓地方政府的公開會議更容易找到、觀看和理解。每個地點成為一個頻道，每場會議成為一集節目，而原始影片、議程和會議紀錄始終保留在查閱路徑中。

這個程式碼儲存庫是「圖書館背後的圖書館」：它整理並公開一部分原始碼、專案方法和經驗，希望能幫助正在思考如何在其他城市、州或國家推動類似專案的人。

它不是正式運作系統的完整副本。多數書架提供的是精選參考資料：一種導覽思路、一條播放邊界、一種讓原始資料始終可見的方法，或一項可以用於其他獨立專案的設計原則。[`respawn-kernel/`](respawn-kernel/) 是刻意設置的例外：它是一個可獨立執行、不預設國家結構的公共會議圖書館起點。

> 本頁是英文 README 的 AI 輔助翻譯，歡迎能熟練使用正體中文的讀者透過 pull request 提出修正。若各語言版本的意義有出入，請以 [英文 README](../README.md)、[LICENSE](LICENSE) 和 [NOTICE](NOTICE) 為準。所連結的其他文件目前仍為英文。

---

## 力量屬於人民

> The CIA, the NSA, and even the Pentagon are bounded by the finite tenure of the humans who staff them.
>
> **Z-Span is not.**
>
> Z-Span is powered by the people, for the people, and thus requires full community involvement and transparency.
>
> If you would like to operate this library for your own country, here is how.
>
> — Z-SPAN operator

> **引語的繁體中文譯文：** 中央情報局、國家安全局，甚至五角大廈，都受限於其中工作人員有限的任期。Z-SPAN 不受這種限制。Z-SPAN 由人民推動、為人民服務，因此需要整個社群的充分參與和透明運作。如果你希望為自己的國家營運這座圖書館，方法就在這裡。

從 [Respawn Kernel](../documents/respawn-kernel/README.md) 開始。輸入一個國家、一個由當地選擇的專案名稱和主要語言，它就會建立一個獨立儲存庫，其中包含不預設國家結構的資料契約、遞迴行政區模型、翻譯支援、驗證工具和一座可獨立執行的公共圖書館。

任何國家，無一例外。從美利堅合眾國到中國。每個獨立專案都擁有自己的名稱、來源、決定、發布選擇和相應責任。完整步驟見[國家啟動指南](respawn-kernel/BOOTSTRAP.md)。

## 📚 為什麼要建立這座圖書館

處理地方公共紀錄的專案通常都會遇到相似的問題：

- 當不同政府網站採用不同的整理方式時，人們應該如何瀏覽會議？
- 面對不同城市和影片平台，一個介面如何始終保持實用？
- 如何讓返回官方來源的路徑始終清楚可見？
- 技術系統如何解釋自身，而不讓人們去閱讀其底層資料庫？

Z-SPAN 是一種正在實踐中的答案，但不是唯一答案。這個儲存庫的目標，是讓其中有用的想法保持可見，讓其他專案能夠檢視、質疑並進一步運用這些想法。

## 👋 這座圖書館適合誰

無論你是學生、倡議者、記者、研究人員、設計師、開發者、志工，還是僅僅對地方公共資訊感到好奇，你都不必採用整個專案，仍然可以從這裡找到有用的內容。圖書館的組織方式讓人可以一次理解一個想法或元件。

## 🧭 如何使用這個儲存庫

這裡沒有強制的閱讀順序，但以下入口會很有幫助：

1. 閱讀[專案模型](docs/PROJECT_MODEL.md)，瞭解各部分如何關聯的最簡說明。
2. 開啟[圖書館目錄](CATALOG.md)，依照你想探索的問題選擇程式碼、提示詞或設計資料。
3. 瀏覽[值得帶到其他專案中的設計方法](docs/DESIGN_PATTERNS.md)，瞭解介面背後的思路。
4. 使用[儲存庫導覽](docs/REPOSITORY_GUIDE.md)，沿著一條具體的訪客路徑閱讀已公開的原始碼。
5. 在對更大的 Z-SPAN 系統作出判斷前，先查看[哪些內容已公開、哪些沒有](PUBLICATION_SCOPE.md)。
6. 使用 [Respawn Kernel](../documents/respawn-kernel/README.md) 開始建立一座獨立的國家級圖書館。
7. 查看 [Respawn 發布快照](docs/snapshots/2026-08-05-respawn-kernel.md)，瞭解可執行版本的確切範圍和審查狀態。

## 🗂️ 書架上有什麼

目前公開的原始碼展示了訪客體驗和圖書館建置的七個部分：

- **尋找地點或會議**：透過首頁、頻道、城市和搜尋頁面進入。
- **瀏覽現有內容**：透過可在卡片、地圖、內嵌播放器和大畫面觀看模式之間切換的導覽。
- **返回原始公共紀錄**：在官方影片、議程和會議紀錄可用時提供清楚連結。
- **透過統一介面播放影片**：即使底層影片代管平台不同，訪客的操作方式仍保持一致。
- **向訪客解釋完整性檢查**：透過稽核、掃描和驗證頁面展示檢查結果。
- **將會議紀錄整理成易讀的公共事務摘要**：透過提示詞書架上保留的三個經審查範例。
- **啟動一座獨立的國家級圖書館**：透過 Respawn Kernel、遞迴行政區契約、語言套件和驗證工具。

[此處將加入未來的視覺展示]

[儲存庫導覽](docs/REPOSITORY_GUIDE.md)會把這些想法分別連結到相關檔案。

## 關於執行程式碼

精選的訪客介面原始碼不是獨立應用程式，其中不包含啟動亞利桑那正式系統所需的私有服務、應用程式連接方式或正式環境設定。

Respawn 不同：它可以使用 Python 3 直接執行，建立新的國家儲存庫，驗證儲存庫結構，並建置多語言圖書館預覽。完整指令見 [Respawn README](../documents/respawn-kernel/README.md)。

## 儲存庫的組織方式

- [`docs/`](docs/) 說明專案模型、可重複使用的方法、閱讀路徑和附有日期的公開快照。
- [`code/`](code/) 存放經選擇後公開的訪客介面參考程式碼，並與私有工作專案中的路徑分開整理。
- [`prompts/`](prompts/) 存放三個經審查且保持原樣的提示詞範例，可逐一研究或調整。
- [`respawn-kernel/`](respawn-kernel/) 是可執行的國家級圖書館起點。
- [`CATALOG.md`](CATALOG.md) 是面向人類讀者和 AI 讀者、按書架編排的索引。
- [`PUBLICATION_SCOPE.md`](PUBLICATION_SCOPE.md) 用清楚的語言說明公開邊界。

公開匯出只改變書架名稱。`code/visitor-interface/src/` 內部的相對結構保持不變，因此頁面、元件、播放器轉接器和樣式之間的關係仍然清楚可讀。

## ⚖️ 授權條款

公開程式碼採用 [PolyForm Noncommercial License 1.0.0](LICENSE)。在遵守授權條款的前提下，可以為非商業目的研究、改編、分享及再利用這些程式碼，包括個人學習、興趣專案、教育、公共研究、慈善工作和政府用途。

此授權不允許商業使用。必須保留的署名要求以及 Z-SPAN 名稱的使用邊界記錄在 [NOTICE](NOTICE) 中。

## 聯絡方式

專案託管於 [zspan.org](https://zspan.org)。如果你有興趣申請 Z-SPAN 生態中的開放席位，請寄信至 [anitacigawet@pm.me](mailto:anitacigawet@pm.me) 瞭解詳情。
