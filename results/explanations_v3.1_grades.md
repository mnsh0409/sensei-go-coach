# Sensei 解說評分（v3.1）— T5 解說集評分與原因分析

* **來源頁面**：[Sensei 解說評分（v3.1）](https://claude.ai/artifact/GMuGNQfqyDZUjpnUk6L7my)
* **評估版本**：`muse-spark-1.3`（`epsilon 0.01`，`commit e4c1ab6`，Explainer Prompt v3.1）
* **已評題數**：20 / 20（10 題 × 解說與問答各 1 項）
* **解說平均**：**3.20 / 5.0**（較 v1 的 2.80 提升 +0.40）
* **問答平均**：**3.30 / 5.0**（較 v1 的 2.20 提升 +1.10）
* **總平均**：**3.25 / 5.0**（較 v1 的 2.50 提升 +0.75；距離 3.5 分 T5 關卡門檻僅差 0.25 分，已完全消除 1–2 分的嚴重誤導題）

## 評分總表

| 題號 | 階段與標籤 | 對局連結 | 解說評分 | 問答評分 | 備註（哪裡有錯，或少了什麼？） |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **E1** | 布局 · 15級（Sensei 白 / 學習者黑） | [OGS 86228104](https://online-go.com/game/86228104) | **4** | **4** | 人稱與棋理正確，清楚點出壓迫 D16（剩 2 氣）及後續 C17 手段；惟問答與解說高度重複，且漏掉 C17 後續、`watch for your reply` 語句略彆扭 |
| **E2** | 布局 · 5級（Sensei 白 / 學習者黑） | [OGS 77324422](https://online-go.com/game/77324422) | **3** | **3** | 人稱正確，但僅背誦距離幾路與「新子有 4 口氣」等表面幾何事實，未向 5 級解釋 C8 的戰術／布局用意及 M3 後為何怕黑 G6 |
| **E3** | 中盤 · 3級（Sensei 黑 / 學習者白） | [OGS 73738029](https://online-go.com/game/73738029) | **3** | **3** | 誠實指出 J16 比 K18 多贏約 6 目，但仍套模板稱虧 6 目的 K18「因 3 級少下而值得學」；問答未解釋為何捨 J16 下 K18，且同段目數（46.7 vs 46.1）小不一致 |
| **E4** | 中盤 · 3段（Sensei 黑 / 學習者白） | [OGS 72393442](https://online-go.com/game/72393442) | **3** | **3** | 解說同時寫「其他手都會吃虧」與「有好幾種合理下法」前後矛盾；問答把「不碰子、距 D12 兩路、有 4 氣」當成想法，且預告下一手從 D10 變成 R7 |
| **E5** | 中盤 · 10級（Sensei 白 / 學習者黑） | [OGS 87962930](https://online-go.com/game/87962930) | **3** | **3** | 人稱正確並列出成 6 子 7 氣，但僅描述斜對 J2/G2 與小飛 F2 的幾何位置，未點出分斷／做活／補強的具體目的；問答照抄解說且漏了 C6 |
| **E6** | 中盤 · 9級（Sensei 黑 / 學習者白） | [OGS 85680630](https://online-go.com/game/85680630) | **3** | **4** | 已修正 v1 的方位與人稱錯誤；解說幾何描述過於冗長，但問答清楚點出「讓右下保持連絡」及白 K6「瞄準下邊」的棋理 |
| **E7** | 官子 · 3級（Sensei 黑 / 學習者白） | [OGS 75402568](https://online-go.com/game/75402568) | **3** | **3** | 解說未明講 G15 是先手便宜；問答前句說 G15 與 P17 目數沒差（皆落後約 8 目），後句卻寫先下 P17 被白下 B15 後落後 10.9 目，前後矛盾且未點出左上 B15 急所 |
| **E8** | 官子 · 1段（Sensei 白 / 學習者黑） | [OGS 87526519](https://online-go.com/game/87526519) | **4** | **4** | 人稱與棋理清楚，明確交代先在 D7 叫吃 C7、等黑 C6 應後再回下 F4 的先手交換次序，僅有少許幾何冗詞 |
| **E9** | 唯一手 · 10級（Sensei 白 / 學習者黑） | [OGS 88898324](https://online-go.com/game/88898324) | **3** | **3** | 人稱已修正，但僅說 C5 緊鄰己子成 5 子 5 氣，未向 10 級解釋若不下 C5 會被黑 B6/D6 如何分斷或吃子；問答幾乎照抄解說 |
| **E10** | 唯一手 · 7級（Sensei 白 / 學習者黑） | [OGS 68278207](https://online-go.com/game/68278207) | **3** | **3** | 人稱已修正且問答對 D11（找空間）的說明合理，但對唯一手 F14 的「想法」仍只是重複「小飛 D15、斜對 E13/E15、5 子 3 氣」的幾何關係，未點出防斷／長出救子 |

## 逐題詳細評分與原因

### E1 · 布局
* **對局與標籤**：[OGS 對局 86228104（跳到第 37 手）](https://online-go.com/game/86228104)｜學習者 15 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 3 手
* **Sensei 對這手的解說：4 分**
  * **優點**：完全修正了 v1 的人稱錯亂（清楚區分 `I played D17` 與 `after your D15`），並具體點出 15 級能懂的關鍵戰術事實：D17 貼住黑棋後，讓旁邊黑 D16 只剩 2 口氣，而白新子有 3 口氣並與 C16 成象步／斜角呼應；同時給出「若黑在 F15 應，白下一步會點入左上 C17（三三）」的具體後續。
  * **小瑕疵**：方位修飾語（`in the upper-left corner (4-5 point)`, `on the top side, 5th line, left half`）略顯機械冗長。
* **問 Sensei (Q&A)：4 分**
  * **優點**：直接回答了 D17 的想法是「以 3 氣壓迫只剩 2 氣的 D16 黑子」。
  * **小瑕疵**：內容與第一段高度重複，且反而把第一段最有價值的後續警告（黑應 F15 後白會下 C17）漏掉了；結尾 `watch for your reply at F15`（注意你自己在 F15 的應手）英文表達略不自然。

### E2 · 布局
* **對局與標籤**：[OGS 對局 77324422（跳到第 25 手）](https://online-go.com/game/77324422)｜學習者 5 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 2 手
* **Sensei 對這手的解說：3 分**
  * **幾何數據取代棋理**：人稱正確，但對 C8 的說明只有「斜對 D7、距離 D5 三路、現在有 4 口氣」。一顆沒貼著別人的棋子本來就有 4 口氣，對 5 級棋手來說這不是棋理，也沒有解釋為何白棋不理右下 R14 而要在左邊肩衝／拆邊 C8。
* **問 Sensei (Q&A)：3 分**
  * **未真正回答「為什麼選 C8 不選 M3」**：回答只說「因為 C8 勝率 44.6% 比 M3 的 43.4% 稍高」，接著唸了一串雙方的子距離幾路（`M3 touches no stones, with your stone at K4 2 points away and my stone at J3 3 points away`——其實 C8 同樣不碰任何子），沒有向 5 級棋手解釋為何下 M3 後被黑搶中央 G6 會讓白棋難受。

### E3 · 中盤
* **對局與標籤**：[OGS 對局 73738029（跳到第 80 手）](https://online-go.com/game/73738029)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰略型｜界限內 8 手
* **Sensei 對這手的解說：3 分**
  * **模板推理矛盾**：相比 v1，這版誠實寫出了「J16 比我這手 K18 多贏約 6 目」，但前一句卻依然套用固定模板寫 `It is worth learning because players around 3k rarely find it and it keeps my winning chances`。在已經贏 40 目的局面下，二路 K18 其實是一手虧 6 目的退讓緩手，3 級棋手不常下它是因為 J16 更自然且目數更多，不應把虧 6 目的緩手稱為「值得學習的好棋」。
* **問 Sensei (Q&A)：3 分**
  * **給出 J16 的盤面細節但未回答核心問題**：具體說明了 J16 貼住白 J17 使其只剩 2 氣（白應 M17），但學習者問的是「你為什麼下 K18 而不是 J16？」，回答最後只說 `so J16 keeps more points`，沒有解釋為何 Sensei 放棄多 6 目的 J16 而選 K18（例如求簡明安全）；且同段內對 J16 的領先目數先寫 `46.7 points`、後寫 `46.1`，數字微幅不一致。

### E4 · 中盤
* **對局與標籤**：[OGS 對局 72393442（跳到第 52 手）](https://online-go.com/game/72393442)｜學習者 3 段｜Sensei 執黑｜學習者執白｜戰略型｜界限內 1 手
* **Sensei 對這手的解說：3 分**
  * **前後文模板直接衝突**：前一句寫 `every other move gives up ground here`（代表此處是界限內唯一手，其他下法都虧），下一句卻接著寫 `This is a quiet position with several reasonable plans`（這是個有好幾種合理下法的平穩局面），兩句話互相矛盾。
  * **對 3 段棋手而言資訊過於表面**：只說「C10 不碰任何子、離 D12 兩路、離 D14 四路、有 4 口氣」，沒有點出左邊大場拆邊兼逼迫 D12 的價值。
* **問 Sensei (Q&A)：3 分**
  * **把座標距離當成「想法」**：回答寫 `the idea is that it touches no stones but comes within 2 points of your stone at D12 while staying 4 points from my stone at D14, leaving my new stone with 4 liberties`，這只是把座標距離再唸一次，完全不是 3 段棋手需要的戰略想法。
  * **預告應手與上段不一致**：第一段解說叫學習者注意在左邊 `D10` 應，問答卻改成叫學習者注意在右邊 `R7` 脫先，且未說明為何下完最佳手 C10 後黑勝率顯示為 `43.4%`。

### E5 · 中盤
* **對局與標籤**：[OGS 對局 87962930（跳到第 137 手）](https://online-go.com/game/87962930)｜學習者 10 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 6 手
* **Sensei 對這手的解說：3 分**
  * **有提供氣數變化但未點明戰術目的**：人稱正確，也指出 H3 與 F2 成小飛、使該塊白棋變成 6 子 7 氣，並給出黑 D5、白 C6 的後續；但未直接向 10 級學習者點明 H3 卡在黑 J2 與 G2 斜角的核心目的（例如連絡補強自己並防黑棋連回／分斷）。
* **問 Sensei (Q&A)：3 分**
  * **照抄首段幾何描述**：面對學習者問「H3 的想法是什麼」，幾乎原封不動把第一段的第一句話再抄一次，沒有補充任何更直白的棋理，且把第一段原本有寫的「黑 D5 後白應 C6」省略了。

### E6 · 中盤
* **對局與標籤**：[OGS 對局 85680630（跳到第 132 手）](https://online-go.com/game/85680630)｜學習者 9 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 4 手
* **Sensei 對這手的解說：3 分**
  * **已修正 v1 錯誤但幾何描述過碎**：修正了 v1 把右邊二路 S7 說成「下邊」及人稱錯亂的問題；但第一句一口氣塞入「斜對 R8、與 Q7 一間跳、與 Q8 小飛、距 Q9 兩路、有 4 口氣」五個幾何事實，卻沒先講這手棋在幹嘛，對 9 級來說較難消化。結尾 `Watch for that reply and keep your answer exact` 語意也稍嫌混亂。
* **問 Sensei (Q&A)：4 分**
  * **清楚點出局部目的與全局流向**：明確回答 S7 的想法是 `my best way to keep my lower right together`（讓右下的棋保持連絡的最佳下法），並指出白下一手 K6 是 `aimed at the bottom side`（瞄準下邊），讓 9 級學習者能看懂「黑補右下、白轉攻下邊」的脈絡，僅 `watch for your next stroke at K6` 英文用詞稍顯生硬。

### E7 · 官子
* **對局與標籤**：[OGS 對局 75402568（跳到第 162 手）](https://online-go.com/game/75402568)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 3 手
* **Sensei 對這手的解說：3 分**
  * **有給出交換次序但未點破「先手」概念**：解說指出「黑下 G15 後，白應 G16，接著黑再回下右上 P17」，其實已經把先手便宜的次序擺出來了，但因沒點出「G15 是先手刺／威脅，白若不應 G16 會有什麼損失」，只唸了斜對 F16、小飛 J14/F17 有 4 氣，3 級棋手較難一眼抓到重點。
* **問 Sensei (Q&A)：3 分**
  * **前後目數自相矛盾且漏掉核心原因**：第一句說選 G15 是因為「它和 P17 一樣讓我落後約 8 目，目數沒有明顯差別（`no meaningful point difference to force me into the corner`）」；但最後一句卻說若先下 P17 叫吃 Q17，白會脫先搶左上 `B15`，結果黑「落後約 10.9 目」——8 目與 10.9 目差了將近 3 目，顯然有明顯差距！真正的棋理是「右上 P17 雖是叫吃但還不是最急，若先走 P17 會被白搶走左上 B15 急所；先在左上／上邊走 G15 才是正確次序」，回答卻沒把這層關聯串起來。

### E8 · 官子
* **對局與標籤**：[OGS 對局 87526519（跳到第 177 手）](https://online-go.com/game/87526519)｜學習者 1 段｜Sensei 執白｜學習者執黑｜戰術型｜界限內 2 手
* **Sensei 對這手的解說：4 分**
  * **先手交換次序清楚**：人稱正確，且清楚說明白棋不直接跟著在 F4 應，而是先在左邊 D7 **叫吃黑 C7**（`puts your stones at C7 in atari`），等黑在 C6 應之後，白棋**再回下下邊 F4**。這對 1 段棋手來說是非常清楚實用的「先手官子交換」示範，僅中間穿插的斜對 E8/C8 等幾何描述稍嫌冗長。
* **問 Sensei (Q&A)：4 分**
  * **修正了 v1 人稱錯誤且對比明確**：清楚對比了 D7（叫吃 C7，領先約 10.8 目）與直接下 F4（與 D4 成一間跳並貼住 F3，黑應左下 D3 後白棋安全、領先約 10.2 目）的差異，對 1 段棋手理解兩者目數與厚薄差距很有幫助。

### E9 · 唯一手
* **對局與標籤**：[OGS 對局 88898324（跳到第 121 手）](https://online-go.com/game/88898324)｜學習者 10 級｜Sensei 執白｜學習者執黑｜唯一手｜界限內 1 手
* **Sensei 對這手的解說：3 分**
  * **未說明「唯一手」的必然原因**：雖然指出 C5 緊鄰己子、斜對黑 D6 與 B6，讓白棋成 5 子 5 氣，且是唯一不吃虧的一手（`a must-play here`），但沒告訴 10 級學習者：黑剛下 B6 威脅到了什麼？如果白不在 C5 補棋（例如脫先），左下白棋是不是會被分斷或死棋？
* **問 Sensei (Q&A)：3 分**
  * **重複首段幾何描述**：回答寫 C5 的用意是 `stay next to my own stones around D4 while pressing diagonally on your stones at D6 and B6, leaving my group there with 5 stones and 5 liberties`，基本上只是把第一段換句話說（且在黑 D6、B6 夾擊下，白 C5 主要是護住自己聯絡與眼位，而非「壓迫 D6 與 B6」）；結尾 `watch for next playing B15` 英文語法也略不自然。

### E10 · 唯一手
* **對局與標籤**：[OGS 對局 68278207（跳到第 55 手）](https://online-go.com/game/68278207)｜學習者 7 級｜Sensei 執白｜學習者執黑｜唯一手｜界限內 1 手
* **Sensei 對這手的解說：3 分**
  * **用幾何關係代替局部急所說明**：黑剛下 E13，與 E15 形成對白子的夾擊／切斷威脅，白下 F14 後「5 顆子只有 3 口氣（`5 stones and 3 liberties`）」，可見這是局部救子／長出防斷的緊氣急所；但解說只唸了「緊鄰己子、斜對 E13 與 E15、與 D15 成小飛」，沒點出「長出防斷／救子」的戰術目的。
* **問 Sensei (Q&A)：3 分**
  * **前半重複幾何、後半應手說明不錯**：後半段說明黑下一手 D11 是 `your most testing reply to find room there`（在左邊尋找空間最有考驗性的應手），這句很有棋感；可惜前半段回答「F14 的想法是什麼」時，依然只是把「小飛 D15、斜對 E13 與 E15、成 5 子 3 氣」再背誦一次，沒有點破局部必須長出連接的理由。

## v1 → v3.1 改進成效與下一步衝刺 3.5+ 分建議

### 1. v3.1 已成功解決的重大問題（總平均從 2.50 升至 3.25）
* **人稱視角 100% 修正**：20 個項目中，`I / my / me`（Sensei）與 `you / your`（學習者）完全一致，徹底消除了 v1 中 7 題把「你」指錯成 Sensei 自己的 2 分致命傷。
* **引擎內部術語全面移除**：不再出現 `prior`、`visits`、`admissible moves within the value bound`、`instructiveness` 等 KataGo 內部數值堆砌。
* **盤面方位與叫吃事實正確注入**：修正了 E6 右邊二路 S7 的方位，並在 E1（壓迫 D16 剩 2 氣）、E8（D7 叫吃 C7）成功結合氣數事實給出 4 分級別的實用解說。

### 2. 距離通過 3.5 分門檻只差一步（3.25 → 3.5+ 的三個具體修正點）
1. **在「問 Sensei（What is the idea...）」中，禁止把「幾何位置關係」當成 `The idea is...`**：
   * 目前當棋子沒有接觸或叫吃時（如 E2、E4、E5、E9、E10），模型會寫出 `the idea is that it touches no stones but comes within 2 points of X... leaving my new stone with 4 liberties`。
   * **修正建議**：Prompt 應規定「幾何關係（小飛、斜對、距離幾路、幾子幾氣）是**手段／盤面狀態**，不是**目的（idea）**」。必須根據棋子關係轉譯出一句圍棋目的，例如：
     * 緊鄰己子／小飛己子且氣數緊（如 E9、E10）→ `connect and strengthen my group so your stones at X and Y cannot cut or capture it`（連接補強己方棋塊，防被分斷或吃子）。
     * 三／四路不碰子且靠近對方子（如 E2 C8、E4 C10）→ `stake out space on the side while pressing your stone at X`（在邊上拆邊／佔據空間，同時逼迫對方棋子）。
     * 先手交換後再回到原處（如 E7 G15→P17、E8 D7→F4）→ 明確使用 `forcing move (sente)`（先手便宜／先手交換，逼對方應一手後再搶回大場）。
2. **修正兩處模板邏輯衝突（E3、E4、E7）**：
   * **E4**：當 `moves_within_bound == 1`（寫了 `every other move gives up ground here`）時，**不要**同時輸出 `This is a quiet position with several reasonable plans`。
   * **E3**：當 Sensei 實際下的手（K18）比引擎最佳手（J16）虧超過 2–3 目（只因大勝 40 目而勝率沒掉）時，不要套用 `It is worth learning because players around 3k rarely find it`，而應誠實說 `Since I am already far ahead, K18 is a simple, safe way to settle the top side, even though J16 would gain about 6 more points`。
   * **E7**：比較兩手棋（G15 vs P17）時，避免前句看當前節點說「目數沒差（皆 -8 目）」、後句看變化圖末端說「下完變 -10.9 目」而不解釋原因；應直接說明「若先在右上 P17 定型，白會搶先在左上 B15 動手，因此先在 G15 交換更划算」。
3. **改善預告學習者下一手的英文句型**：
   * 將對著學習者說的 `watch for your reply at X` / `watch for you to answer at X` 改為 `your main reply to consider next is X` 或 `look to answer at X`，保留 `watch for my [Move]` 用於提醒學習者提防 Sensei 的後續手段。
