# Sensei 解說評分（v4.1）— T5 解說集評分與原因分析

* **來源頁面**：[Sensei 解說評分（v4.1）](https://claude.ai/artifact/CHoiqir2xJdT8N6Y3fuCmr)
* **評估版本**：`muse-spark-1.3`（`epsilon 0.01`，`commit bc8cff9`，Explainer v4.1）
* **已評題數**：20 / 20（10 題 × 解說與問答各 1 項）
* **解說平均**：**3.90 / 5.0**（v1: 2.80 → v3.1: 3.20 → **v4.1: 3.90**）
* **問答平均**：**3.70 / 5.0**（v1: 2.20 → v3.1: 3.30 → **v4.1: 3.70**）
* **總平均**：**3.80 / 5.0**（v1: 2.50 → v3.1: 3.25 → **v4.1: 3.80**，**正式通過 3.5 分 T5 關卡門檻！**）

## 評分總表

| 題號 | 階段與標籤 | 對局連結 | 解說評分 | 問答評分 | 備註（哪裡有錯，或少了什麼？） |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **E1** | 布局 · 15級（Sensei 白 / 學習者黑） | [OGS 86228104](https://online-go.com/game/86228104) | **4** | **4** | 清楚說明虛手會被黑 B16 叫吃 C16，且 D17 是先手（黑應 F15 後白仍可下 C17）；惟解說結尾重複了兩次 `F15 -> C17`，問答把 `forced reply`（被迫應手）誤寫為 `forcing reply` |
| **E2** | 布局 · 5級（Sensei 白 / 學習者黑） | [OGS 77324422](https://online-go.com/game/77324422) | **3** | **3** | 解說同時寫「其他手都會吃虧」與「和 M3 差不多」自相矛盾；解說與問答對非接觸手 C8 仍只報距離與勝率／虛手目數差，未說明布局棋理 |
| **E3** | 中盤 · 3級（Sensei 黑 / 學習者白） | [OGS 73738029](https://online-go.com/game/73738029) | **3** | **3** | 問答清楚點出 J16 貼住 J17（剩 2 氣）且比虛手多 15.9 目（K18 僅 9.8 目），但解說與問答仍未解釋 Sensei 為何放棄多 6 目的 J16 而選 K18 |
| **E4** | 中盤 · 3段（Sensei 黑 / 學習者白） | [OGS 72393442](https://online-go.com/game/72393442) | **3** | **3** | 已修正 v3.1 的模板衝突，但對 3 段仍只描述「C9 不碰子、距 D12 三路、有 4 氣、比虛手多 14 目」；且解說預告白應 C6，問答卻改成預告白應 R7 |
| **E5** | 中盤 · 10級（Sensei 白 / 學習者黑） | [OGS 87962930](https://online-go.com/game/87962930) | **3** | **3** | 解說第一句漏寫座標 `H3`（只寫 `I played on the bottom side...`）；兩段仍僅描述斜對 J2/G2 與小飛 F2 成 6 子 7 氣，未觸發具體死活或分斷理由 |
| **E6** | 中盤 · 9級（Sensei 黑 / 學習者白） | [OGS 85680630](https://online-go.com/game/85680630) | **5** | **4** | 大幅進步！以「若虛手會被白下 S8，使 R8 黑棋只剩 2 氣」一語道破急所，9 級極易理解；問答補充價值 27.5 目，僅略重複且漏了 D11 後續 |
| **E7** | 官子 · 3級（Sensei 黑 / 學習者白） | [OGS 75402568](https://online-go.com/game/75402568) | **4** | **4** | 大幅進步！明確點出 G15 是「先手（sente）」，黑應 G16 後仍可回下 P17，問答也清楚比較兩者對虛手的價值（16.3 vs 15.7 目）；僅解說結尾重複了兩次 `G16 -> P17` |
| **E8** | 官子 · 1段（Sensei 白 / 學習者黑） | [OGS 87526519](https://online-go.com/game/87526519) | **4** | **4** | 清楚說明 D7 先手叫吃 C7、黑應 C6 後白仍可回下 F4（比虛手多 27.3 目 vs F4 的 27.0 目）；惟解說末句 `That leaves F4 next and a likely answer for you is C6 and then I would play F4` 重複且次序略亂 |
| **E9** | 唯一手 · 10級（Sensei 白 / 學習者黑） | [OGS 88898324](https://online-go.com/game/88898324) | **5** | **5** | 完美示範！解說直指「若虛手會被你下 C5 吃掉 2 子」，問答進一步補充「把 C6 與 D5 連成 5 子，氣數從 1 氣變 5 氣」，精準、簡潔且極具教學價值 |
| **E10** | 唯一手 · 7級（Sensei 白 / 學習者黑） | [OGS 68278207](https://online-go.com/game/68278207) | **5** | **4** | 解說直指「若虛手會被你下 F14 吃掉 4 子」非常清楚（5 分）；問答補充「讓 E14 從 1 氣變 3 氣、價值 52.9 目」極佳，僅末句 `watch for your D11... as what comes next` 語句略生硬 |

## 逐題詳細評分與原因

### E1 · 布局
* **對局與標籤**：[OGS 對局 86228104（跳到第 37 手）](https://online-go.com/game/86228104)｜學習者 15 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 2 手
* **Sensei 對這手的解說：4 分**
  * **優點**：新增的「虛手威脅」與「先手（forcing move）」理由非常到位！清楚告訴 15 級學習者：（1）如果白棋虛手，黑會下 B16 叫吃 C16 白子並讓 B15 只剩 2 氣；（2）先下 D17 是先手，黑預期應在 F15，之後白棋**依然可以下到 C17**，比直接下 C17 多得約 1 目。
  * **小瑕疵**：結尾連續兩句寫了完全一樣的變化：`...with your answer expected at F15 and then I still play C17. A likely answer for you is F15 and then I would play C17.`（先手說明句與預設後續句重複）。
* **問 Sensei (Q&A)：4 分**
  * **優點**：完整回答了「為何先下 D17 而不是 C17」（先以 D17 壓迫 2 氣的 D16，等黑應 F15 後白仍可佔 C17；若直接下 C17 雖能讓 C16 從 2 氣變 4 氣，但黑可搶外圍 F14）。
  * **小瑕疵**：將黑棋在 F15 的「被迫應手（forced reply）」誤寫成 `your forcing reply at F15`（語意變成黑 F15 是先手逼迫），且 `get 4 liberties instead of 2 in a 2-stone group with 4 liberties` 略嫌重複。

### E2 · 布局
* **對局與標籤**：[OGS 對局 77324422（跳到第 25 手）](https://online-go.com/game/77324422)｜學習者 5 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 1 手
* **Sensei 對這手的解說：3 分**
  * **前後文自相矛盾**：第二句寫 `I chose it because every other move gives up ground here and it is about the same as M3`——既然「和 M3 差不多（about the same as M3）」，就不可能「其他任何著手都會吃虧（every other move gives up ground here）」。
  * **大場／非接觸手仍缺棋理**：因為 C8 沒有直接叫吃或救子，解說退回描述「斜對 D7、距 D5 三路、有 4 氣、比虛手多 13 目」，未向 5 級棋手說明左邊拆邊兼限制黑 D7 發展的用意。
* **問 Sensei (Q&A)：3 分**
  * **純數據比較**：回答為何選 C8 不選 M3 時，只給出勝率（`45.0%` vs `43.4%`）與對虛手目數（`12.5` vs `12.3` 目），以及下 M3 後黑應 G6 的勝率與目數（且最後同時寫「我勝率 40.6%」與「你下 G6 後勝率 60.3%」，把同一個局面的黑白勝率重複報了兩次），未解釋棋理。

### E3 · 中盤
* **對局與標籤**：[OGS 對局 73738029（跳到第 80 手）](https://online-go.com/game/73738029)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰略型｜界限內 6 手
* **Sensei 對這手的解說：3 分**
  * **優點與不足**：清楚說明 `but J16 gains about 6 points more so for points J16 is the better move`（就目數而言 J16 才是好棋），不再誤導學習者；但對 K18 本身仍只有靜態幾何描述（斜對 J17 與 L17、小飛 H17、4 氣），且未說明為何選了少 6 目的 K18。
* **問 Sensei (Q&A)：3 分**
  * **對 J16 的解說清楚，但未回答為何選 K18**：回答很清楚地指出 J16 的好處（貼住白子使 J17 只剩 2 氣，比虛手多 15.9 目，而 K18 只比虛手多 9.8 目，J16 後白應 M17），但學習者問的是 `Why did you play K18 instead of J16?`，回答開頭只說 `I played K18... for about a 40 point lead for me, while J16... was better for me for about a 46 point lead`，沒有給出選 K18 的理由（例如「因為已經大幅領先 40 目，K18 在二路簡明收官也足以獲勝，但若追求最佳目數應該下 J16」）。

### E4 · 中盤
* **對局與標籤**：[OGS 對局 72393442（跳到第 52 手）](https://online-go.com/game/72393442)｜學習者 3 段｜Sensei 執黑｜學習者執白｜戰略型｜界限內 3 手
* **Sensei 對這手的解說：3 分**
  * **已修正 v3.1 矛盾但仍缺 3 段所需的戰略概念**：修正了 v3.1「唯一手 vs 多種下法」的矛盾；但對 3 段棋手而言，「C9 不碰任何子、離最近的子 D12 三路、新子有 4 氣、勝率從 46% 保持在 46%、比虛手多 14 目」依然只是基礎盤面數據，未點出左邊大場的價值。
* **問 Sensei (Q&A)：3 分**
  * **仍把座標距離當成想法，且預告應手前後不一致**：回答 C9 的想法仍是 `worth 14.3 points compared with passing, putting a 4-liberty stone 3 points away from your stone at D12 while touching no stones`；此外，第一段解說寫 `A likely answer for you is C6 and then I would play E9`，問答卻寫 `What you should watch for next is your reply at R7`，兩段給出的下一手預告不同。

### E5 · 中盤
* **對局與標籤**：[OGS 對局 87962930（跳到第 137 手）](https://online-go.com/game/87962930)｜學習者 10 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 6 手
* **Sensei 對這手的解說：3 分**
  * **首句漏寫座標 `H3`**：英文首句寫 `I played on the bottom side, 3rd line, left half because...`，漏掉了最重要的著手座標 `H3`！
  * **仍為幾何位置描述**：雖然指出下完後該處白棋變成 6 子 7 氣，但沒有觸發「若虛手會如何」或「連接哪兩塊棋／救哪顆子」的具體戰術理由。
* **問 Sensei (Q&A)：3 分**
  * **與首段高度重複**：僅加上「比虛手多 16.4 目、領先 120.6 目」，其餘皆為重複第一段的幾何描述。

### E6 · 中盤
* **對局與標籤**：[OGS 對局 85680630（跳到第 132 手）](https://online-go.com/game/85680630)｜學習者 9 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 3 手
* **Sensei 對這手的解說：5 分**
  * **簡潔精準、極具教學性**：完全捨棄了 v3.1 冗長難懂的五重幾何描述，直接點出核心威脅：`I played S7 on the right side, 2nd line, lower half because if I had passed you could play S8 and my group at R8 next to it would have 2 liberties.` 9 級學習者一讀就懂「不補 S7 的話，白下 S8 會把 R8 黑棋壓成只剩 2 氣」，後續 `K6 -> D11` 也交代得乾淨俐落。
* **問 Sensei (Q&A)：4 分**
  * **優點**：清楚重申 S7 是為了防止白下 S8 把 R8 壓成 2 氣，並補充這手棋價值高達 `27.5 points compared with passing`。
  * **小瑕疵**：內容與第一段高度重疊，且漏掉了第一段提到的黑後續 `D11`（另 `watch for your reply... at K6` 語氣略生硬）。

### E7 · 官子
* **對局與標籤**：[OGS 對局 75402568（跳到第 162 手）](https://online-go.com/game/75402568)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 3 手
* **Sensei 對這手的解說：4 分**
  * **優點**：補上了 v3.1 最缺的關鍵字——**先手（sente）**！明確說明 `it is sente that still lets me play P17 next while giving up nothing since it scores about 1 point more than P17`，讓 3 級棋手立刻明白為何可以先脫先下 G15 再回手下 P17。
  * **小瑕疵**：第一句連續三個 `on the`（`on the top side on the 5th line on the left half`），且結尾同樣出現先手句與後續句重複（`...lets me play P17 next... A likely answer for you is G16 and then I would play P17`）。
* **問 Sensei (Q&A)：4 分**
  * **優點**：彻底修正了 v3.1 前後目數矛盾的問題！清楚說明 G15 比虛手多 16.3 目（白應 G16 後黑仍可下 P17），而直接下 P17 比虛手多 15.7 目（雖叫吃 Q17 並防住白 P14 叫吃 P13，但白會搶到左上角 B15）。
  * **小瑕疵**：`while if I passed there you would play your P14` 的 `there` 指涉稍微不夠直白，結尾 `so I am still losing by points` 略顯多餘。

### E8 · 官子
* **對局與標籤**：[OGS 對局 87526519（跳到第 177 手）](https://online-go.com/game/87526519)｜學習者 1 段｜Sensei 執白｜學習者執黑｜戰術型｜界限內 2 手
* **Sensei 對這手的解說：4 分**
  * **優點**：刪去了 v3.1 多餘的周邊棋子幾何描述，直接聚焦於「D7 叫吃 C7，且是先手（sente），黑應 C6 後白仍可回下 F4」。
  * **小瑕疵**：最後一句 `That leaves F4 next and a likely answer for you is C6 and then I would play F4` 與前文 `you are expected to answer at C6 and then I still play F4` 完全重複，且先寫 `That leaves F4 next` 再寫 `C6 -> F4` 語序略亂。
* **問 Sensei (Q&A)：4 分**
  * **優點**：非常清楚地對比了「先 D7 叫吃（價值 27.3 目）逼黑應 C6 再下 F4」與「直接下 F4（價值 27.0 目）後黑下 D3」的差異，完全符合 1 段棋手的需求。
  * **小瑕疵**：僅末句 `and that is the score of 10.3 points for me after the whole line` 英文句型稍顯生硬。

### E9 · 唯一手
* **對局與標籤**：[OGS 對局 88898324（跳到第 121 手）](https://online-go.com/game/88898324)｜學習者 10 級｜Sensei 執白｜學習者執黑｜唯一手｜界限內 1 手
* **Sensei 對這手的解說：5 分**
  * **極佳示範**：第一句 `I play C5 in the lower-left corner (3-5 point) because if I passed you would play there yourself and capture 2 of my stones` 直白、準確、毫不囉嗦，完美解決了 v3.1 只講幾何卻不講死活的問題，10 級學習者一看就能學會。
* **問 Sensei (Q&A)：5 分**
  * **層次分明的補充解說**：不僅沒有照抄第一段，還進一步補充了關鍵細節：`so my C5 connects my stones at C6 and D5 into one group of 5 stones and leaves it with 5 liberties instead of 1, worth 26.6 points compared with passing`，把「防對手下 C5 提兩子」與「連接 C6、D5 兩塊棋並把氣從 1 口長到 5 口」完整串聯起來，教學效果極佳。

### E10 · 唯一手
* **對局與標籤**：[OGS 對局 68278207（跳到第 55 手）](https://online-go.com/game/68278207)｜學習者 7 級｜Sensei 執白｜學習者執黑｜唯一手｜界限內 1 手
* **Sensei 對這手的解說：5 分**
  * **極佳示範**：第一句 `I played F14 in the center toward the top-left because if I passed you would play there yourself and capture 4 of my stones` 清楚點出這是一手價值連城的救子唯一手，後續 `D11 -> C10` 也簡潔明快。
* **問 Sensei (Q&A)：4 分**
  * **優點**：同樣在問答中補充了極佳的具體盤面事實：`Concretely it gives my stones at E14 3 liberties instead of 1, leaving my group of 5 stones there with 3 liberties next to your stones at E13 and E15`，並指出救回這 4 子價值高達 52.9 目。
  * **小瑕疵**：僅最後一句 `After my F14 with you to play, watch for your D11 on the left side in the upper half as what comes next` 句型稍顯生硬。

## v4.1 總結與再往上微調的收尾建議

### 1. 為什麼 v4.1 能一舉突破 3.5 分關卡（來到 3.80 分）？
* **「虛手威脅（Null-move threat）」、「連回／救子氣數變化（1 氣變 N 氣）」與「先手（Sente）」三大引擎理由發揮關鍵作用**：
  * 在 **E6、E9、E10** 中，模型不再背誦「斜對誰、小飛誰」，而是直接指出「若我不下這裡，你會下 S8 把 R8 壓成 2 氣 / 下 C5 吃我 2 子 / 下 F14 吃我 4 子（讓 E14 從 1 氣變 3 氣）」，直接誕生了 4 個 **5 分**！
  * 在 **E1、E7、E8** 中，明確點出「這手是先手（sente / forcing move），你應了之後我還能再下原本的大場」，讓官子與局部先手交換變得非常容易理解，穩定拿到 **4 分**。

### 2. 若要從 3.80 分進一步推升至 4.2+ 分的 4 個小修補（Low-Hanging Fruit）
1. **消除先手句（sente）與結尾預測句（A likely answer for you is...）的重複（影響 E1、E7、E8）**：
   * 當前文已經輸出 `and it is sente / a forcing move since you are expected to answer at [X] and then I still play [Y]` 時，請程式直接**省略**結尾的 `A likely answer for you is [X] and then I would play [Y]`（在 E1、E7、E8 中這兩句話完全重複，修正後這三題的解說都能直接升上 **5 分**）。
2. **修補兩處小 Bug（影響 E2、E5）**：
   * **E5 解說首句漏座標**：補回 `I played H3 on the bottom side...` 的 `H3`。
   * **E2 解說模板互斥**：當候選著手與次佳手目數差距極小（輸出 `it is about the same as M3`）時，不要同時輸出 `every other move gives up ground here`。
3. **為「非接觸的大場／拆邊／跳出（如 E2 C8、E4 C9）」補充一個簡單的布局分類標籤**：
   * 當沒有叫吃、救子或先手時，若該著手位於三／四路且不碰子（如 C8、C9），與其只寫 `touches no stones with your nearest stone at D12 three points away and my new stone has 4 liberties`，不如根據它與對方子／己方子的距離加上一句簡單的布局定位（例如 `stakes out territory on the left side while approaching/pressing your stone at D7/D12`）。
4. **當 Sensei 下的不是最高目數手時（如 E3 K18 vs J16），給一句簡明理由**：
   * 當勝率保持不變但目數比引擎首選少超過 3 目時（如 E3 已領先 40 目），在問答中加上一句 `Because I am already far ahead by about 40 points, K18 is a safe way to keep the win, even though J16 gains about 6 more points`，就能完美回答「為什麼你下 K18 而不是 J16」。
