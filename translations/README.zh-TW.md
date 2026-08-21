<p align="center">
  <img src="../repository-assets/banner-doodle.png" alt="Z-SPAN 屬於所有人。一座關於地方政治的虛擬圖書館。由人們維護，為人們服務。" width="1000">
</p>

> *Scientia potentia est.*
>
> **知識就是力量。**
>
> — 法蘭西斯·培根

---

[English](../README.md) · [العربية](README.ar.md) · [Español](README.es.md) · [فارسی](README.fa.md) · [Français](README.fr.md) · [हिन्दी](README.hi.md) · [Bahasa Indonesia](README.id.md) · [Filipino](README.fil.md) · [Português (Brasil)](README.pt-BR.md) · [Kiswahili](README.sw.md) · [简体中文](README.zh-CN.md) · [**繁體中文**](README.zh-TW.md) · [Tiếng Việt](README.vi.md)

**一座關於地方政治的虛擬圖書館。**

[前往 zspan.org 造訪 Z-SPAN](https://zspan.org)

✨ **完整公開，人人可用。藉由每個人的幫助持續擴展。**

Z-SPAN 希望讓地方公共會議更容易尋找、觀看和理解。一個地方就是一個頻道，
一場會議就是一集節目，而原始影片、議程和會議紀錄始終保留在查閱路徑中。

這個程式碼儲存庫包含正在運作的圖書館本身：網站、公共 API、會議來源解析器、
處理流程、本機用戶端，以及確保生成內容始終以公共紀錄為依據的檢查機制。
公開整套系統的原因很簡單：由一個人維護的圖書館會隨著那個人而結束；
一座能由其他人檢視、執行、質疑並繼續推進的圖書館則不會。

政府會議來源目錄另行保存在
[National Civics Catalog](https://github.com/anitacigawet/national-civics-catalog)
中。該儲存庫包含持續可用的公共端點及其證據，不包含 Z-SPAN 解析器、逐字稿、
摘要或已處理的會議。Z-SPAN 只是這些資料可以支援的一種應用。

## 觀看完整介紹

[![觀看「Z-SPAN Is Born」——Z-SPAN 專案完整介紹](https://i.ytimg.com/vi/HTpR9jRl314/hqdefault.jpg)](https://www.youtube.com/watch?v=HTpR9jRl314)

[**Z-SPAN Is Born**](https://www.youtube.com/watch?v=HTpR9jRl314) 從維護者的
角度介紹最初的亞利桑那圖書館。影片呈現了 Z-SPAN 最初的構想、各部分如何協作，
以及這條公共道路希望由人們繼續帶往何處。

## 🗺️ 一個逐地建立的全國目錄

亞利桑那是 Z-SPAN 目前處理並公開發布的概念驗證。頻道目錄也為每一州與領地
提供真實的起始結構，依州、縣級等同區域、部落、區域與地方公共機構組織。

綠色書架已有 Z-SPAN 發布的會議。琥珀色書架如實表示工作仍在進行：這個地方
已經進入目錄，但其持續會議來源或 Z-SPAN 解析器仍需完善。任何人都不必等待
邀請，就能幫助自己的社區。

## 🐈 幫助你的家鄉

1. 在 [zspan.org](https://zspan.org) 找到你的州和所在地。
2. 如果書架仍在等待，請點選那隻睡覺的貓。
3. 將簡短的 Markdown 交接說明複製到你已經在使用的 AI 助手中。
4. 回答幾個關於該地點及其官方會議頁面的普通問題。你不需要了解 JSON 或 Git。
5. 如果可以使用 GitHub 工具，助手可以準備一份範圍明確的 pull request 供你確認；
   如果不能，它會改為準備一份完整報告，提交到簡單的 GitHub 表單。

貢獻會進入 National Civics Catalog，由可信的檢查程式和一位真人審核端點及其
證據。它絕不會直接發布到 Z-SPAN。

**Z-SPAN 的三天承諾：**目錄貢獻被接受後，Z-SPAN 會在三天內建立相應解析器，
或發布一項清楚可見的「來源受阻」結果。這項承諾是為了讓來源變得可用，或誠實
說明它為何暫時無法使用；並不代表自動發布由 AI 生成的會議內容。

[閱讀 AI 貢獻說明](https://github.com/anitacigawet/national-civics-catalog/blob/main/contribute/AI-INSTRUCTIONS.md)

## 📚 為什麼要建立這座圖書館

處理地方公共紀錄的專案往往會遇到相同的問題：

- 當政府網站採用不同的組織方式時，人們應該如何瀏覽會議？
- 面對不同地點和影片服務商，一個介面如何始終保持實用？
- 如何讓返回官方來源的路徑始終清楚可見？
- 技術系統如何解釋自身，而不迫使人們閱讀底層資料庫？

Z-SPAN 是一個可行的答案，但不是唯一答案。這個儲存庫的目標是讓整個專案保持
可見，讓使用它的人能夠檢視、質疑並把它帶得更遠。

## 👋 這座圖書館適合誰

無論你是學生、行動者、記者、研究人員、設計師、開發者、志工，還是只是對
地方公共資訊感到好奇，都不需要採用整個專案才能在這裡找到有用的東西。
圖書館的組織方式讓人可以一次理解一個想法或元件，也可以一次加入一個地方。

## 🗂️ 這個儲存庫如何組織

- [council_navigator](../02_Core_Project/council_navigator/) — 網站、公共 API、
  本機會議快取和公共頻道目錄。
- [parsers](../02_Core_Project/council_navigator/parsers/) — 針對特定來源的行事曆
  解析器，將目錄端點轉換成統一的會議結構。
- [zspan_pipeline](../02_Core_Project/zspan_pipeline/) — 處理佇列，將會議錄影
  轉換成有依據、可審查的材料。
- [zspan_cli](../02_Core_Project/zspan_cli/) — 讓人們在自己的電腦和工作區中使用
  Z-SPAN 的本機用戶端。
- [prompts](../02_Core_Project/prompts/) — 處理流程採用的已公開綜合規約。

National Civics Catalog 維持為獨立儲存庫，讓人們可以改善來源目錄而不改動
Z-SPAN 應用，也讓其他專案能出於完全不同的目的使用同一批端點。

## 本專案堅持什麼

以下是專案為自己設定的約束，而不只是願望：

- **不對公職人員作傾向性評論。** 他們的話按原文呈現，註明說話者和來源。
  判斷由你作出。
- **不彙整一般公民的資料。** 本專案關注的是履行公共職責的官員；不會為在
  公共麥克風前發言的居民建立檔案。
- **閱讀永遠不設門檻。** 閱讀已發布的公共紀錄內容不需要付費、訂閱、登入或註冊。
- **不最佳化參與度。** 沒有無限資訊流、推薦演算法或煽動憤怒的機制。
  紀錄刻意保持平靜。
- **任何內容發布前都由真人審查。** 處理可以自動化，發布不能。
- **依設計用於非商業用途。** 授權條款把這條界線寫進了結構中。

## 🏛️ 初創維護

Z-SPAN 始於亞利桑那，由
[@anitacigawet](https://github.com/anitacigawet) 維護。對來源目錄的貢獻會在
National Civics Catalog 中獲得署名；Z-SPAN 的實作則繼續在這裡單獨審查和維護。

## ⚖️ 授權條款

已發布程式碼採用
[PolyForm Noncommercial License 1.0.0](../LICENSE)。在遵守授權條款的前提下，
可出於非商業目的研究、修改、分享和重複使用，包括個人學習、興趣專案、教育、
公共研究、慈善工作和政府用途。

此授權不允許商業使用。必須保留的聲明以及 Z-SPAN 名稱的使用界線記錄在
[NOTICE](../NOTICE) 中。

## 聯絡方式

專案託管於 [zspan.org](https://zspan.org)。歡迎透過本儲存庫的
[問題追蹤器](https://github.com/anitacigawet/Z-SPAN/issues) 提交問題和可重現的錯誤報告。

---

## Z-SPAN 三位一體

![Z-SPAN 三位一體：網際網路負責傳遞，公民紀錄提供根基，人們讓它持續存在](../repository-assets/zspan-trinity.svg)

---

> CIA、NSA，甚至五角大廈，都受限於其中工作人員有限的任期。
>
> **Z-SPAN 不受此限。**
>
> Z-SPAN 由人民推動、為人民服務，因此需要社群的充分參與和透明運作。
>
> — Z-SPAN 維護者

---

## 🌌 把這個想法帶得更遠

National Civics Catalog 依州組織，讓來源目錄可以擴展到全美，而不要求任何人
採用 Z-SPAN 的介面或處理方式。你可以使用這些端點建立社區行事曆、研究工具、
無障礙專案、課堂資源，或任何這裡尚未有人想到的東西。

這個想法的價值並不來自它屬於某一個應用，而來自人們可以不斷找到新方法，
讓公共紀錄更容易接觸。
