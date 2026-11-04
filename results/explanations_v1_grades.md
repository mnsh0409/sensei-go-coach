# Sensei 解說評分（v1）— T5 解說集評分與原因分析

- **來源頁面**：[Sensei 解說評分（v1）](https://claude.ai/artifact/QGJXtKQP2eHsB3weZRdeHH)
- **評估版本**：`muse-spark-1.3`（`epsilon 0.01`，`commit 879c877`）
- **已評題數**：20 / 20（10 題 × 解說與問答各 1 項）
- **解說平均**：**2.80 / 5.0**
- **問答平均**：**2.20 / 5.0**（若 E6 問答從寬給 2 分則為 **2.30**）
- **總平均**：**2.50 / 5.0**（**未通過 3.5 分 T5 關卡門檻**）

## 評分總表

| 題號 | 階段與標籤 | 對局連結 | 解說評分 | 問答評分 | 備註（哪裡有錯，或少了什麼？） |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **E1** | 布局 · 15級（Sensei 白 / 學習者黑） | [OGS 86228104](https://online-go.com/game/86228104) | **2** | **2** | 解說與問答皆把 `you` 指錯為白方；解說全為引擎內部數值堆砌，問答對 15 級直接丟出 `aji` 與四個座標卻未解釋威脅 |
| **E2** | 布局 · 5級（Sensei 白 / 學習者黑） | [OGS 77324422](https://online-go.com/game/77324422) | **3** | **3** | 解說純堆砌 `prior`/`visits`/`instructiveness`，手數錯標且末句邏輯矛盾；問答人稱正確但僅重複數字與空話 |
| **E3** | 中盤 · 3級（Sensei 黑 / 學習者白） | [OGS 73738029](https://online-go.com/game/73738029) | **2** | **3** | 解說把已贏 40 多目時虧 6.1 目的緩手 K18 誤導為「引擎高度評價的戰略範例」；問答修正了目數說法但無棋理 |
| **E4** | 中盤 · 3段（Sensei 黑 / 學習者白） | [OGS 72393442](https://online-go.com/game/72393442) | **3** | **2** | 解說純報讀引擎內部指標；問答把 `you` 指錯為黑方，且誤把同手替代選點 C10 當成 C9 的後續手段（follow-up） |
| **E5** | 中盤 · 10級（Sensei 白 / 學習者黑） | [OGS 87962930](https://online-go.com/game/87962930) | **3** | **2** | 解說純數字堆砌且手數錯標；問答把 `you` 指錯為領先 123.5 目的白方，完全未解釋戰術內容 |
| **E6** | 中盤 · 9級（Sensei 黑 / 學習者白） | [OGS 85680630](https://online-go.com/game/85680630) | **3** | **1** | 解說純數字堆砌；問答把 `you` 指錯為黑方，且將右邊二路的 **S7** 錯寫成「下邊（lower edge）」 |
| **E7** | 官子 · 3級（Sensei 黑 / 學習者白） | [OGS 75402568](https://online-go.com/game/75402568) | **3** | **3** | 人稱正確但全為數字，未解釋為何脫先不應 P17 而搶 G15，以及 P17 後白下 B15 的戰術關聯 |
| **E8** | 官子 · 1段（Sensei 白 / 學習者黑） | [OGS 87526519](https://online-go.com/game/87526519) | **3** | **2** | 解說純數字堆砌；問答寫 `you still keep 98.2 percent... for White` 把學習者錯當成白方，且無 1 段所需的官子棋理 |
| **E9** | 唯一手 · 10級（Sensei 白 / 學習者黑） | [OGS 88898324](https://online-go.com/game/88898324) | **3** | **2** | 解說純列舉各選點勝率與訪問數；問答把 `you` 指錯為白方（`keep you suppressed`），且用空話敷衍局部唯一手 |
| **E10** | 唯一手 · 7級（Sensei 白 / 學習者黑） | [OGS 68278207](https://online-go.com/game/68278207) | **3** | **2** | 解說純數字堆砌；問答把 `you` 指錯為白方（`top choice for you... keep your lead`），且未解釋 F14 與 F15 的棋形差異 |

## 逐題詳細評分與原因

### E1 · 布局
- **對局與標籤**：[OGS 對局 86228104（跳到第 37 手）](https://online-go.com/game/86228104)｜學習者 15 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 3 手
- **Sensei 對這手的解說：2 分**
  - **人稱指錯對象**：第 38 手 D17 是執白的 Sensei 下的，但原文第一句寫 `On move 38 after Black played D15 you played D17 for White`，把執黑的學習者誤稱為下白棋的人。
  - **內部指標堆砌、15 級無法理解**：全文都在背誦 `prior 26.9 percent`、`86 visits`、`3 admissible moves within the value bound`、`instructiveness 0.60` 等系統內部欄位，沒有解釋任何圍棋棋理。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：學習者執黑，回答卻寫 `keeping your large lead. It settles your stones and pressures Black at the same time`，把學習者當成大幅領先的白方。
  - **超出棋力且說理不清**：對 15 級初學者直接拋出術語 `aji`（餘味）並列出四個座標（`C17, B17, B16 and E16`），卻未說明局部究竟有什麼斷點或威脅。

### E2 · 布局
- **對局與標籤**：[OGS 對局 77324422（跳到第 25 手）](https://online-go.com/game/77324422)｜學習者 5 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 1 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌與邏輯矛盾**：全文只有勝率、目數、`prior`、`visits` 與 `instructiveness`，完全沒解釋 P3 與 M3 在棋盤上的作用。此外，前文才說 P3 的先驗機率只有 4.2%（人類直覺極少下，卻是價值界限內唯一可接受的一手），末句卻因 `instructiveness` 為 `-0.00` 而推論 `the engine does not like it much more than a 5k player would`，前後邏輯矛盾；且把第 25 手黑 R14 誤寫為 `on move 26`。
- **問 Sensei (Q&A)：3 分**
  - **人稱正確但內容空泛**：這題人稱正確（`I played P3`），但只把勝率與目數差再唸一遍，並以空泛的 `P3 keeps a little more balance while M3 lets Black seize the initiative` 結尾，沒向 5 級學習者解釋為何白下 M3 會太鬆或留下弱點、讓黑棋能搶先手 G6。

### E3 · 中盤
- **對局與標籤**：[OGS 對局 73738029（跳到第 80 手）](https://online-go.com/game/73738029)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰略型｜界限內 7 手
- **Sensei 對這手的解說：2 分**
  - **具誤導性的數據解讀**：黑棋此時已大勝 40 多目，勝率頂在 99.9% 的天花板。實際上 K18 比引擎首選 J16 **虧了 6.1 目**（`+40.2` vs `+46.3`），且 K18 的先驗機率（`21.3%`）比 J16（`18.5%`）更高；解說卻只因勝率損失顯示為 `0.0%` 及 `instructiveness 1.45`，就誤導稱 `K18 is a good strategic example to learn because the engine rates it highly even though a player of about 3k strength would be less likely to choose it`，把大優局面下虧 6 目的安全緩手吹捧成高深戰略。
- **問 Sensei (Q&A)：3 分**
  - **修正了目數結論但缺乏棋理**：回答中誠實承認 J16 才是引擎首選、K18 讓掉了約 6 目以求安全，並指出 J16 後白應 M17；但依然只停留在報目數，沒有解釋 J16 與 K18 在上邊的棋形與圍空／封鎖差異。

### E4 · 中盤
- **對局與標籤**：[OGS 對局 72393442（跳到第 52 手）](https://online-go.com/game/72393442)｜學習者 3 段｜Sensei 執黑｜學習者執白｜戰術型｜界限內 2 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：通篇照唸 `prior`、`winrate`、`score lead minus 0.0`、`68 visits`、`value loss of only 0.5 percent winrate 0.005`、`instructiveness 2.37`，完全沒有說明左邊 C9、C10、B5、D9、C8 的棋理，3 段棋手從中得不到任何有效資訊。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：Sensei 執黑、學習者執白，回答卻寫 `settle your shape and press White there... watch for White to tenuki as the engine replies R7, so be ready to answer on the right side next`，把執白的學習者當成黑方。
  - **棋理描述不合理**：C9 與 C10 是左邊同一手棋的相鄰替代選點（差一路），回答卻寫 `keeping good follow ups like C10 in reserve`，誤把替代選點當成下完 C9 之後保留的後續手段。

### E5 · 中盤
- **對局與標籤**：[OGS 對局 87962930（跳到第 137 手）](https://online-go.com/game/87962930)｜學習者 10 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 7 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：雖然標籤指出這是 `instructiveness 3.35` 的高教學性戰術局面，但解說只報了 `+123.5 score lead`、`89 visits`、`7 admissible moves`，完全沒提 H3 與 D5 在局部戰術上發生了什麼事（且把第 137 手黑 A8 誤寫成 `on move 138`）。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：Sensei 執白、學習者執黑，回答卻對學習者說 `keeping a 100.0 percent winrate for you with a 123.5 point lead... After H3 watch for Black D6 as the engine reply. Stay calm and solid next`，把學習者當成白方。
  - **內容空洞**：只寫「佔一手簡單厚實的位置、保持安定」，完全沒有回答這個戰術局面的具體想法。

### E6 · 中盤
- **對局與標籤**：[OGS 對局 85680630（跳到第 132 手）](https://online-go.com/game/85680630)｜學習者 9 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 3 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：把 S7、K6、S8、D11、J2 的 `prior` 與 `visits` 全部唸了一遍，沒有任何一句話解釋棋盤上的死活或對殺關係。
- **問 Sensei (Q&A)：1 分**
  - **圍棋方位錯誤**：座標 **S7** 位於棋盤**右邊二路**（S 為第 18 線），回答卻寫 `stay active on the lower edge`（在下邊保持積極），把右邊與下邊（K2/K6 一帶）搞混。
  - **人稱指錯對象**：Sensei 執黑、學習者執白，回答卻對學習者說 `0.8 percent winrate for you... make White prove the capture. Watch for White's K6 next... removes your follow up chances`，把執白的學習者當成落後的黑方。

### E7 · 官子
- **對局與標籤**：[OGS 對局 75402568（跳到第 162 手）](https://online-go.com/game/75402568)｜學習者 3 級｜Sensei 執黑｜學習者執白｜戰術型｜界限內 4 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：只列出 G15 與 P17 的勝率、先驗機率與訪問次數，沒有解釋為何白下右上 Q17 時，黑棋不應在 P17 而是脫先下左中腹的 G15。
- **問 Sensei (Q&A)：3 分**
  - **人稱正確但純報數據**：學習者問「為什麼下 G15 而不是 P17」，回答只說「因為 G15 是 2.6%、P17 是 2.0%，下 P17 後白會強力下 B15（白勝率 98.2%）」，完全沒解釋 G15 和左邊 B15 之間有什麼關係（例如 G15 是否先手防住了 B15 的手段，或左邊死活為何比右上 P17 更大）。

### E8 · 官子
- **對局與標籤**：[OGS 對局 87526519（跳到第 177 手）](https://online-go.com/game/87526519)｜學習者 1 段｜Sensei 執白｜學習者執黑｜戰術型｜界限內 2 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：全部在報 D7 與 F4 的 `winrate`、`score lead`、`prior`、`visits` 與 `instructiveness`，沒有向 1 段棋手說明為何黑下 F3 時白不跟著在 F4 擋，而是脫先下 D7（另下完白 D7 後寫 `for White to move` 也有語病）。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：Sensei 執白、學習者執黑，回答卻寫 `I have shown F4 on the board where you still keep 98.2 percent and about 10.2 points for White and Black's reply is D3`，把 `you` 指錯成白方。
  - **缺乏段位所需的棋理**：對 1 段棋手只說「F4 看起來最自然，D7 只是目數上稍微厚實一點點」，沒解釋 D7 與 F4/D3 的先後手官子價值。

### E9 · 唯一手
- **對局與標籤**：[OGS 對局 88898324（跳到第 121 手）](https://online-go.com/game/88898324)｜學習者 10 級｜Sensei 執白｜學習者執黑｜戰術型｜界限內 1 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：人稱正確（`I played C5`），但把 C5 與其他無關的脫先點（N18、P19、M14）的勝率和個位數訪問次數全部唸出來，卻沒告訴 10 級學習者黑下 B6 後白 C5 在局部是在擋什麼、救哪塊棋。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：Sensei 執白、學習者執黑，卻對學習者說 `After C5 you should watch for Black B15 next, which the engine already wants to answer with to keep you suppressed`，把學習者當成白方。
  - **想法極度空泛**：這題是 `91.2% prior` 的局部唯一手，回答卻只寫 `The idea is to hang on and force the least bad continuation rather than collapse immediately`，完全是看勝率（3.7%）編出來的套話，毫無局部棋理。

### E10 · 唯一手
- **對局與標籤**：[OGS 對局 68278207（跳到第 55 手）](https://online-go.com/game/68278207)｜學習者 7 級｜Sensei 執白｜學習者執黑｜唯一手｜界限內 1 手
- **Sensei 對這手的解說：3 分**
  - **純數字堆砌**：只比較了 F14 與 F15 的 `prior`、`visits`、`winrate` 與 `score lead`，沒有解釋黑 E13 之後，白下 F14 與 F15 在棋形（如扳／擋／退）上的具體差異。
- **問 Sensei (Q&A)：2 分**
  - **人稱指錯對象**：Sensei 執白、學習者執黑，回答卻寫 `F14 is the engine's clear top choice for you... keeps your large advantage... Watch for that reply [Black D11] and answer calmly to keep your lead`，把學習者當成白方。
  - **空泛描述**：只看 F14 勝率高於 F15 就猜測「F14 是下得厚實、不要用 F15 更用力地推進」，沒有真正的棋形解說。

## 系統性問題總結（供模型調優參考）

1. **「你（you）」人稱視角嚴重錯亂（共 7 題中標）**：
   - 當問題是 `What is the idea behind [Move], and what should I watch for next?`（E1、E4、E5、E6、E9、E10）以及 E8 的問答時，模型把提問的「學習者（對手）」當成了「下這手棋的 Sensei 自己」，導致反覆對執黑的學習者說「這手棋能保持你的大幅領先、壓迫黑棋、小心黑棋下一手反擊」等錯位語句。
2. **首段解說淪為 KataGo JSON 欄位朗讀機（10 題全中）**：
   - 所有題目的第一段解說都在背誦 `prior`、`visits`、`value loss`、`admissible moves within the value bound`、`instructiveness` 等系統內部過濾指標，對任何棋力的學習者來說都不是圍棋解說，頂多只能拿 3 分。
3. **缺乏棋盤空間與棋形理解，靠勝率高低硬編萬用套話**：
   - 模型並未真正根據局部棋形（如扳、長、拆邊、補斷、先手官子）來解釋，而是看到勝率高／領先多就寫「保持安定、簡明守住優勢」，看到勝率低就寫「撐住局面、避免立刻崩潰」，甚至在 E6 把右邊二路的 **S7** 說成「下邊（lower edge）」，在 E3 把勝率頂到 99.9% 時虧 6 目的緩手當成「好戰略範例」。
